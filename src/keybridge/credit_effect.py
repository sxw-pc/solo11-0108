"""Credit disposition, future balance use, refund fulfillment and dispute audits."""

import json
from contextlib import closing
from datetime import datetime, timezone

from .credit import _read, _time
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
    note = db.execute('SELECT n.*,c.party,c.currency,c.disposition,c.refunders_json '
                      'FROM credit_note n JOIN credit_contract c ON c.id=n.contract_id WHERE n.id=?',
                      (note_id,)).fetchone()
    if not note:
        raise ValueError('unknown customer credit note')
    return note


def _approved(db, note_id):
    row = db.execute('SELECT decision FROM credit_approval WHERE note_id=?', (note_id,)).fetchone()
    return bool(row and row['decision'] == 'approve')


def _original_unchanged(db, note, decision, current_run):
    try:
        current = _invoice(db, note['invoice_id'], current_run)
    except ValueError:
        return False
    return (current['paid_cents'], current['outstanding_cents']) == (
        decision['paid_at_decision'], decision['outstanding_at_decision'])


def _sum(db, table, where, params):
    return db.execute(f'SELECT COALESCE(SUM(cents),0) FROM {table} WHERE {where}', params).fetchone()[0]


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
        old_offsets = db.execute('SELECT COALESCE(SUM(d.offset_cents),0) FROM credit_decision d '
                                 'JOIN credit_note n ON n.id=d.note_id WHERE n.invoice_id=?',
                                 (note['invoice_id'],)).fetchone()[0]
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
        run = _current_run(db)
        if not _original_unchanged(db, note, decision, run):
            raise ValueError('original invoice settlement changed; credit disputed')
        target = _invoice(db, target_id, run)
        if (target['party'], target['currency']) != (note['party'], note['currency']):
            raise ValueError('future invoice crosses customer or currency boundary')
        if _settlement_time(target['issued_at']) <= _time(note['issued_at']):
            raise ValueError('carry can only offset a future invoice')
        if at < _settlement_time(target['issued_at']):
            raise ValueError('credit application predates future invoice issuance')
        used = _sum(db, 'credit_application', 'note_id=?', (note_id,))
        if amount > decision['carry_cents'] - used:
            raise ValueError('credit application exceeds available customer balance')
        other_offsets = db.execute('SELECT COALESCE(SUM(d.offset_cents),0) FROM credit_decision d '
                                   'JOIN credit_note n ON n.id=d.note_id WHERE n.invoice_id=?',
                                   (target_id,)).fetchone()[0]
        target_used = _sum(db, 'credit_application', 'target_invoice_id=?', (target_id,))
        if amount > target['outstanding_cents'] - other_offsets - target_used:
            raise ValueError('credit application exceeds future invoice outstanding')
        db.execute('INSERT INTO credit_application VALUES(?,?,?,?,?,?,?)',
                   (ident, note_id, target_id, amount, item['applied_at'], sha, raw))
    return {'application_id': ident, 'note_id': note_id, 'target_invoice_id': target_id,
            'applied_cents': amount, 'remaining_cents': decision['carry_cents'] - used - amount,
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
        if not _original_unchanged(db, note, decision, _current_run(db)):
            raise ValueError('original invoice settlement changed; credit disputed')
        paid = _sum(db, 'credit_refund', 'note_id=?', (note_id,))
        if amount > decision['refund_cents'] - paid:
            raise ValueError('refund exceeds remaining obligation')
        db.execute('INSERT INTO credit_refund VALUES(?,?,?,?,?,?,?,?)',
                   (ident, note_id, amount, item['paid_at'], actor, proof, sha, raw))
    return {'refund_id': ident, 'note_id': note_id, 'paid_cents': amount,
            'refund_due_cents': decision['refund_cents'] - paid - amount}


def audit_credit(db_path, note_id, as_of):
    at = _time(as_of)
    with closing(connect(db_path)) as db, db:
        note = _note(db, note_id)
        approval = db.execute('SELECT decision FROM credit_approval WHERE note_id=?', (note_id,)).fetchone()
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        revocation = db.execute('SELECT status FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone()
        run_row = db.execute('SELECT run_id FROM settlement_head WHERE id=1').fetchone()
        current_run = run_row['run_id'] if run_row else None
        available = (decision['carry_cents'] - _sum(db, 'credit_application', 'note_id=?', (note_id,))) if decision else 0
        refund_due = (decision['refund_cents'] - _sum(db, 'credit_refund', 'note_id=?', (note_id,))) if decision else 0
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
        elif not current_run or not _original_unchanged(db, note, decision, current_run):
            status, reason = 'disputed_settlement', 'original invoice bank settlement changed after credit decision'
            available = 0
        elif note['disposition'] == 'carry':
            status = 'expired' if at >= _time(note['expires_at']) and available > 0 else (
                'consumed' if available == 0 else 'available')
            reason = 'unused customer balance expired' if status == 'expired' else 'customer balance disposition'
            if status == 'expired':
                available = 0
        elif refund_due > 0:
            status, reason = 'refund_due', 'approved refund obligation not yet fully paid'
        else:
            status, reason = 'refund_fulfilled' if decision['refund_cents'] else 'offset_applied', 'credit obligation closed'
        cursor = db.execute('INSERT INTO credit_audit(note_id,as_of,status,available_cents,refund_due_cents,current_run_id,reason) '
                            'VALUES(?,?,?,?,?,?,?)', (note_id, as_of, status, available, refund_due, current_run, reason))
    return {'audit_id': cursor.lastrowid, 'note_id': note_id, 'status': status,
            'available_cents': available, 'refund_due_cents': refund_due,
            'current_settlement_run_id': current_run, 'reason': reason}


def credit_report(db_path, note_id):
    with closing(connect(db_path)) as db:
        note = _note(db, note_id)
        approval = db.execute('SELECT id,reviewer,decision,proof_sha,approved_at,source_sha FROM credit_approval WHERE note_id=?',
                              (note_id,)).fetchone()
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        revocation = db.execute('SELECT id,actor,revoked_at,reason,status,source_sha FROM credit_revocation WHERE note_id=?',
                                (note_id,)).fetchone()
        return {'note': {key: note[key] for key in ('id','invoice_id','contract_id','issuer','cents','issued_at',
                                                   'expires_at','supersedes','reason','source_sha','party','currency','disposition')},
                'approval': dict(approval) if approval else None,
                'decision': dict(decision) if decision else None,
                'applications': [dict(x) for x in db.execute('SELECT id,target_invoice_id,cents,applied_at,source_sha '
                                                            'FROM credit_application WHERE note_id=? ORDER BY id', (note_id,))],
                'refunds': [dict(x) for x in db.execute('SELECT id,cents,paid_at,actor,bank_proof_sha,source_sha '
                                                       'FROM credit_refund WHERE note_id=? ORDER BY id', (note_id,))],
                'revocation': dict(revocation) if revocation else None,
                'audits': [dict(x) for x in db.execute('SELECT * FROM credit_audit WHERE note_id=? ORDER BY id', (note_id,))]}
