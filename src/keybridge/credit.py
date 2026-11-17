"""Immutable credit terms, note issuance, independent approval and withdrawal."""

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .credit_store import connect
from .settlement import cents


def _read(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), raw, hashlib.sha256(raw).hexdigest()


def _time(value):
    at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('credit timestamp requires timezone')
    return at


def _invoice_time(value):
    """Legacy settlement timestamps without zones are interpreted as UTC."""
    at = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def _roles(value, name):
    if (not isinstance(value, list) or not value or len(set(value)) != len(value) or
            any(not isinstance(x, str) or not x for x in value)):
        raise ValueError(f'invalid {name} roles')


SETTLEMENT_CHANGE_POLICIES = ('freeze_review', 'reallocate_unspent')


def register_contract(db_path, path):
    item, raw, sha = _read(path)
    ident, party, currency, disposition = (str(item[key]) for key in
                                           ('contract_id', 'party', 'currency', 'disposition'))
    policy = str(item.get('settlement_change_policy', 'freeze_review'))
    if (not all((ident, party, currency)) or disposition not in ('carry', 'refund') or
            policy not in SETTLEMENT_CHANGE_POLICIES):
        raise ValueError('invalid credit contract')
    for role in ('issuers', 'approvers', 'refunders'):
        _roles(item[role], role)
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha FROM credit_contract WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('credit contract id reused with changed terms')
            return {'contract_id': ident, 'idempotent': True}
        db.execute('INSERT INTO credit_contract VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (ident, party, currency, disposition, policy, json.dumps(item['issuers']),
                    json.dumps(item['approvers']), json.dumps(item['refunders']), sha, raw))
    return {'contract_id': ident, 'party': party, 'currency': currency,
            'disposition': disposition, 'settlement_change_policy': policy}


def issue_note(db_path, path):
    item, raw, sha = _read(path)
    ident, invoice_id, contract_id, issuer, reason = (str(item[key]) for key in
                                                     ('note_id', 'invoice_id', 'contract_id', 'issuer', 'reason'))
    amount = cents(item['amount'])
    issued, expires = _time(item['issued_at']), _time(item['expires_at'])
    if not all((ident, invoice_id, contract_id, issuer, reason)) or issued >= expires:
        raise ValueError('invalid credit note')
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha FROM credit_note WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('credit note id reused with changed source')
            return {'note_id': ident, 'idempotent': True}
        contract = db.execute('SELECT * FROM credit_contract WHERE id=?', (contract_id,)).fetchone()
        invoice = db.execute('SELECT * FROM settlement_invoice WHERE id=?', (invoice_id,)).fetchone()
        if not contract or not invoice:
            raise ValueError('credit note requires contract and known invoice')
        if issuer not in json.loads(contract['issuers_json']):
            raise ValueError('issuer lacks credit authority')
        if (invoice['party'], invoice['currency']) != (contract['party'], contract['currency']):
            raise ValueError('credit contract party or currency differs from invoice')
        if issued < _invoice_time(invoice['issued_at']):
            raise ValueError('credit note cannot precede invoice issuance')
        if amount > invoice['cents']:
            raise ValueError('credit note exceeds invoice face amount')
        supersedes = item.get('supersedes')
        if supersedes:
            prior = db.execute('SELECT invoice_id FROM credit_note WHERE id=?', (supersedes,)).fetchone()
            revoked = db.execute('SELECT status FROM credit_revocation WHERE note_id=?', (supersedes,)).fetchone()
            if not prior or prior['invoice_id'] != invoice_id or not revoked or revoked['status'] != 'accepted':
                raise ValueError('corrected credit requires accepted unused prior revocation')
        db.execute('INSERT INTO credit_note VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (ident, invoice_id, contract_id, issuer, amount, item['issued_at'],
                    item['expires_at'], supersedes, reason, sha, raw))
    return {'note_id': ident, 'invoice_id': invoice_id, 'cents': amount, 'status': 'pending_approval'}


def review_note(db_path, path):
    item, raw, sha = _read(path)
    ident, note_id, reviewer, decision, proof = (str(item[key]) for key in
                                                ('approval_id', 'note_id', 'reviewer', 'decision', 'proof_sha'))
    at = _time(item['approved_at'])
    if not all((ident, note_id, reviewer, proof)) or decision not in ('approve', 'reject'):
        raise ValueError('invalid credit approval')
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha FROM credit_approval WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('approval id reused with changed evidence')
            return {'approval_id': ident, 'idempotent': True}
        note = db.execute('SELECT * FROM credit_note WHERE id=?', (note_id,)).fetchone()
        if not note or db.execute('SELECT 1 FROM credit_approval WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('note missing or already reviewed')
        if db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('revoked note cannot receive new approval')
        contract = db.execute('SELECT * FROM credit_contract WHERE id=?', (note['contract_id'],)).fetchone()
        if reviewer == note['issuer'] or reviewer not in json.loads(contract['approvers_json']):
            raise ValueError('independent authorized credit reviewer required')
        if at < _time(note['issued_at']) or at >= _time(note['expires_at']):
            raise ValueError('credit approval falls outside note validity')
        db.execute('INSERT INTO credit_approval VALUES(?,?,?,?,?,?,?,?)',
                   (ident, note_id, reviewer, decision, proof, item['approved_at'], sha, raw))
    return {'approval_id': ident, 'note_id': note_id, 'decision': decision}


def revoke_note(db_path, path):
    item, raw, sha = _read(path)
    ident, note_id, actor, reason = (str(item[key]) for key in
                                    ('revocation_id', 'note_id', 'actor', 'reason'))
    revoked_at = _time(item['revoked_at'])
    if not all((ident, note_id, actor, reason)):
        raise ValueError('invalid credit revocation')
    with closing(connect(db_path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT source_sha,status FROM credit_revocation WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('revocation id reused with changed evidence')
            return {'revocation_id': ident, 'status': old['status'], 'idempotent': True}
        note = db.execute('SELECT * FROM credit_note WHERE id=?', (note_id,)).fetchone()
        if not note or db.execute('SELECT 1 FROM credit_revocation WHERE note_id=?', (note_id,)).fetchone():
            raise ValueError('credit note missing or already revoked')
        if revoked_at < _time(note['issued_at']):
            raise ValueError('revocation cannot precede note issuance')
        contract = db.execute('SELECT approvers_json FROM credit_contract WHERE id=?',
                              (note['contract_id'],)).fetchone()
        if actor not in json.loads(contract['approvers_json']):
            raise ValueError('revocation requires authorized credit reviewer')
        decision = db.execute('SELECT * FROM credit_decision WHERE note_id=?', (note_id,)).fetchone()
        latest = db.execute('SELECT total_offset_cents FROM credit_readjudication WHERE note_id=? '
                            'ORDER BY id DESC LIMIT 1', (note_id,)).fetchone()
        total_offset = (latest['total_offset_cents'] if latest else
                        decision['offset_cents'] if decision else 0)
        used = (total_offset > 0 or
                db.execute('SELECT 1 FROM credit_application WHERE note_id=?', (note_id,)).fetchone() or
                db.execute('SELECT 1 FROM credit_refund WHERE note_id=?', (note_id,)).fetchone())
        status = 'disputed' if used else 'accepted'
        db.execute('INSERT INTO credit_revocation VALUES(?,?,?,?,?,?,?,?)',
                   (ident, note_id, actor, item['revoked_at'], reason, status, sha, raw))
    return {'revocation_id': ident, 'note_id': note_id, 'status': status,
            'reason': 'already used credit needs compensating action' if status == 'disputed' else 'unused credit canceled'}
