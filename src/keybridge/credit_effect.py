"""Credit disposition, future balance use, refund fulfillment and dispute audits.

A settlement run is append-only, so a late bank reversal produces a new invoice
decision without touching the run a credit was first judged against.  When that
decision changes, the contract's ``settlement_change_policy`` decides what may
happen to the credit:

``freeze_review``
    Freeze the note as ``disputed_settlement``.  Existing applications and paid
    refunds stay intact, but no unused amount is automatically spent or paid.
``reallocate_unspent``
    Recompute only the part of the note that has not produced an economic effect,
    first offsetting the current unpaid amount of the source invoice, then
    keeping the remainder in the contract's original disposition.

Every readjudication is an append-only record bound to the original
``credit_decision``, the old and new settlement runs, the current source invoice
decision, the contract digest and the operator's reason.  Applications already
made against future invoices and refunds already paid are never deleted or
rewritten.  The four economic effects -- source-invoice offset, future
applications, paid refunds and the remaining available/refund obligation -- may
never exceed the note face.
"""

import json
from contextlib import closing
from datetime import datetime, timezone

from .credit import SETTLEMENT_CHANGE_POLICIES, _read, _time
from .credit_store import connect
from .settlement import cents


def _settlement_time(value):
    """Legacy settlement fixture timestamps without zones are interpreted as UTC."""
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _current_run(db):
    row = db.execute('SELECT run_id FROM settlement_head WHERE id=1').fetchone()
    if not row:
        raise ValueError('settlement decision missing')
    return row['run_id']


def _invoice(db, invoice_id, run_id):
    row = db.execute('SELECT i.*,d.state,d.paid_cents,d.outstanding_cents FROM settlement_invoice i '
                     'JOIN settlement_invoice_decision d ON d.invoice_id=i.id AND d.run_id=? WHERE i.id=?',
                     (run_id, invoice_id)).fetchone()
    if not row or row['state'] == 'disputed':
        raise ValueError('invoice settlement evidence missing or disputed')
    return row


def _note(db, note_id):
    note = db.execute('SELECT n.*,c.party,c.currency,c.disposition,c.refunders_json,c.source_sha AS '
                      'contract_sha,c.settlement_change_policy FROM credit_note n '
                      'JOIN credit_contract c ON c.id=n.contract_id WHERE n.id=?',
                      (note_id,)).fetchone()
    if not note:
        raise ValueError('unknown customer credit note')
    return note


def _approved(db, note_id):
    row = db.execute('SELECT decision FROM credit_approval WHERE note_id=?', (note_id,)).fetchone()
    return bool(row and row['decision'] == 'approve')


def _sum(db, table, where, params):
    return db.execute(f'SELECT COALESCE(SUM(cents),0) FROM {table} WHERE {where}', params).fetchone()[0]


def _latest_readjudication(db, note_id):
    return db.execute('SELECT * FROM credit_readjudication WHERE note_id=? ORDER BY id DESC LIMIT 1',
                      (note_id,)).fetchone()


def _decision_state(db, note, decision):
    """The adjudicated snapshot that currently governs the note."""
    latest = _latest_readjudication(db, note['id'])
    if latest:
        return (latest['new_run_id'], latest['invoice_paid_cents'],
                latest['invoice_outstanding_cents'], latest)
    return (decision['settlement_run_id'], decision['paid_at_decision'],
            decision['outstanding_at_decision'], None)


def _effective(db, note, decision, latest):
    """Economic totals of the latest adjudication, without touching old evidence.

    ``carry_cents`` and ``refund_cents`` are gross buckets: applications and paid
    refunds are subtracted from their own bucket and are never rewritten.  A
    readjudication stores gross bucket amounts too, so the same arithmetic holds
    before and after it.
    """
    applied = _sum(db, 'credit_application', 'note_id=?', (note['id'],))
    refunded = _sum(db, 'credit_refund', 'note_id=?', (note['id'],))
    if latest:
        total_offset, carry, refund = (latest['total_offset_cents'], latest['carry_cents'],
                                       latest['refund_cents'])
    else:
        total_offset, carry, refund = (decision['offset_cents'], decision['carry_cents'],
                                       decision['refund_cents'])
    if min(total_offset, carry, refund) < 0:
        raise ValueError('credit ledger is inconsistent with its adjudicated disposition')
    if note['disposition'] == 'carry':
        if refund != 0:
            raise ValueError('credit ledger is inconsistent with its adjudicated disposition')
        available, refund_due = carry - applied, 0
    else:
        if carry != 0:
            raise ValueError('credit ledger is inconsistent with its adjudicated disposition')
        available, refund_due = 0, refund - refunded
    if min(available, refund_due) < 0:
        raise ValueError('credit ledger is inconsistent with its adjudicated disposition')
    return {'total_offset_cents': total_offset,
            'added_offset_cents': total_offset - decision['offset_cents'],
            'carry_cents': carry, 'refund_cents': refund, 'applied_cents': applied,
            'refunded_cents': refunded, 'available_cents': available,
            'refund_due_cents': refund_due}


def _settlement_change(db, note, decision):
    """Compare the current run with the last adjudicated snapshot.

    Returns ``aligned`` when the source invoice still matches what the latest
    decision/readjudication saw, and ``reason`` describing an evidence failure.
    """
    run = _current_run(db)
    base_run, base_paid, base_out, latest = _decision_state(db, note, decision)
    try:
        invoice = _invoice(db, note['invoice_id'], run)
        error = None
    except ValueError as exc:
        invoice, error = None, str(exc)
    changed = error is not None or (invoice['paid_cents'], invoice['outstanding_cents']) != (
        base_paid, base_out)
    return {'run_id': run, 'base_run_id': base_run, 'latest': latest, 'invoice': invoice,
            'error': error, 'changed': changed, 'aligned': not changed}


def _policy(db, note):
    return note['settlement_change_policy'] or 'freeze_review'


def _invoice_credit_offsets(db, invoice_id, exclude_note=None):
    """Cumulative source-invoice offsets recorded against an invoice.

    A readjudication can raise a note's offset after it was first decided, so the
    latest readjudication -- not the original decision -- is authoritative here.
    """
    total = 0
    rows = db.execute('SELECT d.note_id,d.offset_cents FROM credit_decision d '
                      'JOIN credit_note n ON n.id=d.note_id WHERE n.invoice_id=?',
                      (invoice_id,)).fetchall()
    for row in rows:
        if exclude_note and row['note_id'] == exclude_note:
            continue
        latest = _latest_readjudication(db, row['note_id'])
        total += latest['total_offset_cents'] if latest else row['offset_cents']
    return total


def _other_offsets(db, invoice_id, note_id):
    return _invoice_credit_offsets(db, invoice_id, exclude_note=note_id)


def _check_face(note, total_offset, applied, refunded, available, refund_due):
    if min(total_offset, applied, refunded, available, refund_due) < 0:
        raise ValueError('credit readjudication produced a negative economic effect')
    if total_offset + applied + refunded + available + refund_due > note['cents']:
        raise ValueError('credit readjudication exceeds the note face amount')


def _policy_totals(note, effective, invoice, decision, occupied, changed):
    """Compute the contract-authorized totals for one readjudication.

    ``occupied`` is every claim already recorded against the source invoice --
    other notes' offsets, this note's own prior offsets and any future-invoice
    applications -- so reallocation can never push combined credit effects
    beyond the current bank unpaid amount.  ``changed`` says whether the current
    run differs from the last adjudicated snapshot; without new evidence no
    unspent amount may move.
    ``freeze_review`` changes no amount: existing effects stay and the credit is
    either confirmed against the restored evidence or frozen for human review.
    ``reallocate_unspent`` moves only the amount that has not yet produced an
    economic effect, capped by the current source invoice unpaid amount.
    """
    if note['settlement_change_policy'] == 'freeze_review':
        # Freeze is anchored on the original decision: only evidence matching the
        # amount that decision saw can lift a dispute, even across several runs.
        aligned = ((invoice['paid_cents'], invoice['outstanding_cents']) ==
                   (decision['paid_at_decision'], decision['outstanding_at_decision']))
        if aligned:
            return {'added': 0, 'status': 'confirmed', 'total_offset': effective['total_offset_cents'],
                    'carry': effective['carry_cents'], 'refund': effective['refund_cents'],
                    'available': effective['available_cents'],
                    'refund_due': effective['refund_due_cents'],
                    'detail': 'source settlement restored to the original decision; credit remains available'}
        return {'added': 0, 'status': 'disputed_settlement',
                'total_offset': effective['total_offset_cents'], 'carry': effective['carry_cents'],
                'refund': effective['refund_cents'], 'available': 0, 'refund_due': 0,
                'detail': 'credit frozen pending human review after source invoice settlement change'}
    capacity = invoice['outstanding_cents'] - occupied
    if capacity < 0:
        raise ValueError('existing source invoice credit effects exceed current outstanding')
    unspent = (effective['available_cents'] if note['disposition'] == 'carry'
               else effective['refund_due_cents'])
    added = min(unspent, max(0, capacity)) if changed else 0
    total_offset = effective['total_offset_cents'] + added
    # The gross buckets always split the note face with the offset bucket:
    # cents = offset + carry for a carry note, offset + refund for a refund note.
    if note['disposition'] == 'carry':
        carry, refund = effective['carry_cents'] - added, 0
    else:
        carry, refund = 0, effective['refund_cents'] - added
    available = carry - effective['applied_cents']
    refund_due = refund - effective['refunded_cents']
    if min(available, refund_due) < 0 or total_offset + carry + refund != note['cents']:
        raise ValueError('credit readjudication cannot preserve the contract disposition within the note face')
    return {'added': added, 'status': 'reallocated' if added else 'confirmed',
            'total_offset': total_offset, 'carry': carry, 'refund': refund,
            'available': available, 'refund_due': refund_due,
            'detail': (f'{added} unspent cents reallocated to the current source invoice unpaid amount'
                       if added else
                       'no unspent credit can move under the current settlement evidence')}


def decide_credit(db_path, note_id, as_of):
    at = _time(as_of)
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        note = _note(db, note_id)
        old = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        if old:
            return {'decision_id': old['id'], 'note_id': note_id, 'status': old['status'],
                    'offset_cents': old['offset_cents'], 'carry_cents': old['carry_cents'],
                    'refund_cents': old['refund_cents'], 'idempotent': True}
        if not _approved(db, note_id):
            raise ValueError('credit note lacks independent approval')
        if db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('revoked credit note cannot be disposed')
        if at < _time(note['issued_at']) or at >= _time(note['expires_at']):
            raise ValueError('credit note expired or not yet issued')
        run = _current_run(db)
        invoice = _invoice(db, note['invoice_id'], run)
        old_offsets = _other_offsets(db, note['invoice_id'], note_id)
        applied = _sum(db, 'credit_application', 'target_invoice_id=?', (note['invoice_id'],))
        remaining_due = invoice['outstanding_cents'] - old_offsets - applied
        if remaining_due < 0:
            raise ValueError('existing credits exceed current invoice outstanding; reconcile first')
        offset = min(note['cents'], remaining_due)
        excess = note['cents'] - offset
        carry = excess if note['disposition'] == 'carry' else 0
        refund = excess if note['disposition'] == 'refund' else 0
        reason = ('offset against unpaid invoice; excess carried to customer' if carry else
                  'offset against unpaid invoice; excess creates refund obligation' if refund else
                  'credit fully offsets unpaid invoice')
        decision_id = db.execute('INSERT INTO credit_decision(note_id,settlement_run_id,paid_at_decision,'
                                 'outstanding_at_decision,offset_cents,carry_cents,refund_cents,status,reason) '
                                 'VALUES(?,?,?,?,?,?,?,?,?)',
                                 (note_id, run, invoice['paid_cents'], invoice['outstanding_cents'],
                                  offset, carry, refund, 'decided', reason)).lastrowid
    return {'decision_id': decision_id, 'note_id': note_id, 'settlement_run_id': run,
            'status': 'decided', 'offset_cents': offset, 'carry_cents': carry,
            'refund_cents': refund, 'reason': reason}


def readjudicate_credit(db_path, path):
    """Append the contract-authorized disposition after a settlement change.

    The evidence file names the note, carries a reason, and may pin the expected
    old run, contract digest and prior readjudication.  Everything the record
    needs is computed and validated inside one transaction: a changed or
    incomplete evidence file and any amount above the note face reject the whole
    readjudication without touching applications, refunds or audits.
    """
    item, raw, sha = _read(path)
    try:
        ident, note_id, reason = (str(item[key]) for key in ('readjudication_id', 'note_id', 'reason'))
    except KeyError as exc:
        raise ValueError('invalid credit readjudication evidence') from exc
    if not all((ident, note_id, reason)):
        raise ValueError('invalid credit readjudication evidence')
    if item.get('at') is not None:
        _time(item['at'])
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT * FROM credit_readjudication WHERE source_id=?', (ident,)).fetchone()
        if old:
            if old['evidence_sha'] != sha:
                raise ValueError('readjudication id reused with changed evidence')
            return {'readjudication_id': old['id'], 'note_id': old['note_id'],
                    'status': old['status'], 'idempotent': True}
        note = _note(db, note_id)
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        if not decision:
            raise ValueError('credit decision missing')
        if not _approved(db, note_id):
            raise ValueError('credit note lacks independent approval')
        if db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('revoked credit note cannot be readjudicated')
        if _policy(db, note) not in SETTLEMENT_CHANGE_POLICIES:
            raise ValueError('unknown settlement change policy')
        if item.get('expected_contract_sha') is not None and str(item['expected_contract_sha']) != note['contract_sha']:
            raise ValueError('credit contract changed since readjudication evidence was prepared')
        change = _settlement_change(db, note, decision)
        run = change['run_id']
        latest = change['latest']
        base_run = change['base_run_id']
        if item.get('expected_old_run_id') is not None and int(item['expected_old_run_id']) != base_run:
            raise ValueError('credit readjudication old settlement run changed')
        if item.get('expected_readjudication_id') is not None:
            prior_id = latest['id'] if latest else None
            if int(item['expected_readjudication_id']) != (prior_id or 0):
                raise ValueError('credit readjudication evidence targets a stale prior record')
        if run == base_run:
            raise ValueError('no new settlement run to readjudicate')
        duplicate = db.execute('SELECT 1 FROM credit_readjudication WHERE note_id=? AND new_run_id=?',
                               (note_id, run)).fetchone()
        if duplicate:
            raise ValueError('settlement run already readjudicated for this credit note')
        if change['invoice'] is None:
            raise ValueError(f'source invoice settlement evidence unavailable: {change["error"]}')
        effective = _effective(db, note, decision, latest)
        invoice = change['invoice']
        policy = _policy(db, note)
        outcome = _policy_totals(note, effective, invoice, decision,
                                 effective['total_offset_cents'] +
                                 _other_offsets(db, note['invoice_id'], note_id) +
                                 _sum(db, 'credit_application', 'target_invoice_id=?',
                                      (note['invoice_id'],)), change['changed'])
        added, status = outcome['added'], outcome['status']
        total_offset, carry, refund = (outcome['total_offset'], outcome['carry'], outcome['refund'])
        available, refund_due = outcome['available'], outcome['refund_due']
        detail = outcome['detail']
        _check_face(note, total_offset, effective['applied_cents'], effective['refunded_cents'],
                    available, refund_due)
        readjudication_id = db.execute(
            'INSERT INTO credit_readjudication(source_id,note_id,decision_id,prior_readjudication_id,'
            'policy,old_run_id,new_run_id,invoice_id,invoice_state,invoice_paid_cents,'
            'invoice_outstanding_cents,contract_sha,status,added_offset_cents,total_offset_cents,'
            'carry_cents,refund_cents,applied_cents,refunded_cents,available_cents,'
            'refund_due_cents,evidence_sha,raw,reason) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (ident, note_id, decision['id'], latest['id'] if latest else None, policy, base_run, run,
             note['invoice_id'], invoice['state'], invoice['paid_cents'],
             invoice['outstanding_cents'], note['contract_sha'], status, added, total_offset,
             carry, refund, effective['applied_cents'], effective['refunded_cents'], available,
             refund_due, sha, raw, reason)).lastrowid
    return {'readjudication_id': readjudication_id, 'note_id': note_id, 'policy': policy,
            'old_run_id': base_run, 'new_run_id': run, 'status': status,
            'added_offset_cents': added, 'total_offset_cents': total_offset,
            'carry_cents': carry, 'refund_cents': refund,
            'applied_cents': effective['applied_cents'], 'refunded_cents': effective['refunded_cents'],
            'available_cents': available, 'refund_due_cents': refund_due,
            'reason': detail}


def _actionable(db, note, decision):
    """Return the effective ledger or reject an action the contract does not allow."""
    change = _settlement_change(db, note, decision)
    latest = change['latest']
    policy = _policy(db, note)
    if change['aligned']:
        if latest and latest['status'] == 'disputed_settlement':
            raise ValueError('original invoice settlement changed; credit disputed')
        return _effective(db, note, decision, latest)
    if policy == 'freeze_review':
        raise ValueError('original invoice settlement changed; credit disputed')
    raise ValueError('original invoice settlement changed; readjudication required before use')


def apply_credit(db_path, path):
    item, raw, sha = _read(path)
    ident, note_id, target_id = (str(item[key]) for key in
                                 ('application_id', 'note_id', 'target_invoice_id'))
    amount = cents(item['amount'])
    at = _time(item['applied_at'])
    if not all((ident, note_id, target_id)):
        raise ValueError('invalid credit application')
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha FROM credit_application WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('credit application id reused with changed source')
            return {'application_id': ident, 'idempotent': True}
        note = _note(db, note_id)
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        if not decision or not _approved(db, note_id) or note['disposition'] != 'carry':
            raise ValueError('customer balance is not approved for carry')
        if db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('revoked or disputed credit cannot be applied')
        if at < _time(note['issued_at']) or at >= _time(note['expires_at']):
            raise ValueError('customer balance expired or predates credit')
        effective = _actionable(db, note, decision)
        run = _current_run(db)
        target = _invoice(db, target_id, run)
        if (target['party'], target['currency']) != (note['party'], note['currency']):
            raise ValueError('future invoice crosses customer or currency boundary')
        if _settlement_time(target['issued_at']) <= _time(note['issued_at']):
            raise ValueError('carry can only offset a future invoice')
        if at < _settlement_time(target['issued_at']):
            raise ValueError('credit application predates future invoice issuance')
        if amount > effective['available_cents']:
            raise ValueError('credit application exceeds available customer balance')
        other_offsets = _invoice_credit_offsets(db, target_id)
        target_used = _sum(db, 'credit_application', 'target_invoice_id=?', (target_id,))
        # `other_offsets` already includes this note's own offset when the
        # target is the source invoice, so no double subtraction is needed.
        if amount > target['outstanding_cents'] - other_offsets - target_used:
            raise ValueError('credit application exceeds future invoice outstanding')
        db.execute('INSERT INTO credit_application VALUES(?,?,?,?,?,?,?)',
                   (ident, note_id, target_id, amount, item['applied_at'], sha, raw))
    return {'application_id': ident, 'note_id': note_id, 'target_invoice_id': target_id,
            'applied_cents': amount, 'remaining_cents': effective['available_cents'] - amount,
            'target_effective_outstanding_cents': target['outstanding_cents'] - other_offsets - target_used - amount}


def fulfill_refund(db_path, path):
    item, raw, sha = _read(path)
    ident, note_id, actor, proof = (str(item[key]) for key in
                                    ('refund_id', 'note_id', 'actor', 'bank_proof_sha'))
    amount = cents(item['amount'])
    paid_at = _time(item['paid_at'])
    if not all((ident, note_id, actor, proof)):
        raise ValueError('invalid credit refund evidence')
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha FROM credit_refund WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('refund id reused with changed evidence')
            return {'refund_id': ident, 'idempotent': True}
        note = _note(db, note_id)
        if paid_at < _time(note['issued_at']):
            raise ValueError('refund evidence predates credit note')
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        if not decision or note['disposition'] != 'refund' or actor not in json.loads(note['refunders_json']):
            raise ValueError('refund obligation or authorized payer missing')
        if db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('revoked or disputed note cannot be refunded')
        effective = _actionable(db, note, decision)
        if amount > effective['refund_due_cents']:
            raise ValueError('refund exceeds remaining obligation')
        db.execute('INSERT INTO credit_refund VALUES(?,?,?,?,?,?,?,?)',
                   (ident, note_id, amount, item['paid_at'], actor, proof, sha, raw))
    return {'refund_id': ident, 'note_id': note_id, 'paid_cents': amount,
            'refund_due_cents': effective['refund_due_cents'] - amount}


def audit_credit(db_path, note_id, as_of):
    at = _time(as_of)
    with closing(connect(db_path)) as db, db:
        note = _note(db, note_id)
        approval = db.execute('SELECT decision FROM credit_approval WHERE note_id=?', (note_id,)).fetchone()
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        revocation = db.execute('SELECT status FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone()
        run_row = db.execute('SELECT run_id FROM settlement_head WHERE id=1').fetchone()
        current_run = run_row['run_id'] if run_row else None
        latest = _latest_readjudication(db, note_id) if decision else None
        change = _settlement_change(db, note, decision) if decision and current_run else None
        # Only an adjudication that actually matches the current run governs the audit.
        readjudication_id = latest['id'] if latest and change and change['aligned'] else None
        try:
            effective = _effective(db, note, decision, latest) if decision else {
                'available_cents': 0, 'refund_due_cents': 0, 'refund_cents': 0}
        except ValueError:
            # A ledger whose recorded effects already exceed its disposition is
            # reported as disputed, never netted or partially adjusted.
            effective = {'available_cents': 0, 'refund_due_cents': 0, 'refund_cents': 0}
            readjudication_id = None
            consistent = False
        else:
            consistent = True
        available, refund_due = effective['available_cents'], effective['refund_due_cents']
        if at < _time(note['issued_at']):
            status, reason = 'not_yet_effective', 'credit note has not been issued at audit time'
            available = 0
        elif not approval:
            status, reason = 'pending_approval', 'independent credit approval missing'
        elif approval['decision'] == 'reject':
            status, reason = 'rejected', 'credit reviewer rejected note'
        elif revocation:
            status, reason = ('disputed_revocation' if revocation['status'] == 'disputed' else 'revoked',
                              'credit withdrawal has prior economic effects' if revocation['status'] == 'disputed'
                              else 'unused credit canceled')
            available = 0
        elif not decision:
            status, reason = ('expired', 'approved note expired before disposition') if at >= _time(note['expires_at']) else (
                'pending_disposition', 'approved note not yet allocated')
        elif not consistent:
            status, reason = 'disputed_ledger', 'recorded credit effects exceed the adjudicated disposition'
            available = 0
            refund_due = 0
        elif not current_run:
            status, reason = 'disputed_settlement', 'settlement decision missing for credit audit'
            available = 0
        elif latest and latest['status'] == 'disputed_settlement':
            status, reason = 'disputed_settlement', 'credit frozen by contract until a readjudication releases it'
            available = 0
            refund_due = 0
        elif change['changed']:
            if change['invoice'] is None:
                status, reason = ('disputed_settlement',
                                  'source invoice settlement evidence missing or disputed in the current run')
            elif _policy(db, note) == 'reallocate_unspent':
                status, reason = ('readjudication_required',
                                  'new settlement run changed source invoice; unspent credit awaits readjudication')
            else:
                status, reason = ('disputed_settlement',
                                  'original invoice bank settlement changed after credit decision')
            available = 0
            refund_due = 0
        elif note['disposition'] == 'carry':
            status = 'expired' if at >= _time(note['expires_at']) and available > 0 else (
                'consumed' if available == 0 else 'available')
            reason = 'unused customer balance expired' if status == 'expired' else 'customer balance disposition'
            if status == 'expired':
                available = 0
        elif refund_due > 0:
            status, reason = 'refund_due', 'approved refund obligation not yet fully paid'
        else:
            status, reason = 'refund_fulfilled' if effective['refund_cents'] else 'offset_applied', 'credit obligation closed'
        cursor = db.execute('INSERT INTO credit_audit(note_id,as_of,status,available_cents,refund_due_cents,'
                            'current_run_id,readjudication_id,reason) VALUES(?,?,?,?,?,?,?,?)',
                            (note_id, as_of, status, available, refund_due, current_run,
                             readjudication_id, reason))
    return {'audit_id': cursor.lastrowid, 'note_id': note_id, 'status': status,
            'available_cents': available, 'refund_due_cents': refund_due,
            'current_settlement_run_id': current_run, 'readjudication_id': readjudication_id,
            'reason': reason}


def credit_report(db_path, note_id):
    with closing(connect(db_path)) as db:
        note = _note(db, note_id)
        approval = db.execute('SELECT id,reviewer,decision,proof_sha,approved_at,source_sha FROM credit_approval WHERE note_id=?',
                              (note_id,)).fetchone()
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        revocation = db.execute('SELECT id,actor,revoked_at,reason,status,source_sha FROM credit_revocation WHERE note_id=?',
                                (note_id,)).fetchone()
        return {'note': {key: note[key] for key in ('id','invoice_id','contract_id','issuer','cents','issued_at',
                                                   'expires_at','supersedes','reason','source_sha','party','currency',
                                                   'disposition')},
                'contract': {'id': note['contract_id'], 'source_sha': note['contract_sha'],
                             'settlement_change_policy': note['settlement_change_policy']},
                'approval': dict(approval) if approval else None,
                'decision': dict(decision) if decision else None,
                'readjudications': [dict(x) for x in db.execute(
                    'SELECT id,source_id,decision_id,prior_readjudication_id,policy,old_run_id,new_run_id,'
                    'invoice_id,invoice_state,invoice_paid_cents,invoice_outstanding_cents,contract_sha,'
                    'status,added_offset_cents,total_offset_cents,carry_cents,refund_cents,applied_cents,'
                    'refunded_cents,available_cents,refund_due_cents,evidence_sha,reason,created_at '
                    'FROM credit_readjudication WHERE note_id=? ORDER BY id', (note_id,))],
                'applications': [dict(x) for x in db.execute('SELECT id,target_invoice_id,cents,applied_at,source_sha '
                                                            'FROM credit_application WHERE note_id=? ORDER BY id', (note_id,))],
                'refunds': [dict(x) for x in db.execute('SELECT id,cents,paid_at,actor,bank_proof_sha,source_sha '
                                                       'FROM credit_refund WHERE note_id=? ORDER BY id', (note_id,))],
                'revocation': dict(revocation) if revocation else None,
                'audits': [dict(x) for x in db.execute('SELECT * FROM credit_audit WHERE note_id=? ORDER BY id', (note_id,))]}
