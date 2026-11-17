"""Customer credit obligations, separate from payment matching and migration."""

from .settlement_store import connect as settlement_connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_contract(
  id TEXT PRIMARY KEY, party TEXT NOT NULL, currency TEXT NOT NULL, disposition TEXT NOT NULL,
  settlement_change_policy TEXT NOT NULL DEFAULT 'freeze_review',
  issuers_json TEXT NOT NULL, approvers_json TEXT NOT NULL, refunders_json TEXT NOT NULL,
  source_sha TEXT NOT NULL, raw BLOB NOT NULL, UNIQUE(party,currency));
CREATE TABLE IF NOT EXISTS credit_note(
  id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES settlement_invoice(id),
  contract_id TEXT NOT NULL REFERENCES credit_contract(id), issuer TEXT NOT NULL,
  cents INTEGER NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  supersedes TEXT REFERENCES credit_note(id), reason TEXT NOT NULL,
  source_sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credit_approval(
  id TEXT PRIMARY KEY, note_id TEXT NOT NULL UNIQUE REFERENCES credit_note(id), reviewer TEXT NOT NULL,
  decision TEXT NOT NULL, proof_sha TEXT NOT NULL, approved_at TEXT NOT NULL,
  source_sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credit_decision(
  id INTEGER PRIMARY KEY, note_id TEXT NOT NULL UNIQUE REFERENCES credit_note(id),
  settlement_run_id INTEGER NOT NULL REFERENCES settlement_run(id),
  paid_at_decision INTEGER NOT NULL, outstanding_at_decision INTEGER NOT NULL,
  offset_cents INTEGER NOT NULL, carry_cents INTEGER NOT NULL, refund_cents INTEGER NOT NULL,
  status TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS credit_application(
  id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES credit_note(id),
  target_invoice_id TEXT NOT NULL REFERENCES settlement_invoice(id), cents INTEGER NOT NULL,
  applied_at TEXT NOT NULL, source_sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credit_refund(
  id TEXT PRIMARY KEY, note_id TEXT NOT NULL REFERENCES credit_note(id), cents INTEGER NOT NULL,
  paid_at TEXT NOT NULL, actor TEXT NOT NULL, bank_proof_sha TEXT NOT NULL,
  source_sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credit_revocation(
  id TEXT PRIMARY KEY, note_id TEXT NOT NULL UNIQUE REFERENCES credit_note(id), actor TEXT NOT NULL,
  revoked_at TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL,
  source_sha TEXT NOT NULL, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS credit_readjudication(
  id INTEGER PRIMARY KEY, source_id TEXT NOT NULL UNIQUE,
  note_id TEXT NOT NULL REFERENCES credit_note(id),
  decision_id INTEGER NOT NULL REFERENCES credit_decision(id),
  prior_readjudication_id INTEGER REFERENCES credit_readjudication(id),
  policy TEXT NOT NULL, old_run_id INTEGER NOT NULL REFERENCES settlement_run(id),
  new_run_id INTEGER NOT NULL REFERENCES settlement_run(id),
  invoice_id TEXT NOT NULL REFERENCES settlement_invoice(id), invoice_state TEXT NOT NULL,
  invoice_paid_cents INTEGER NOT NULL, invoice_outstanding_cents INTEGER NOT NULL,
  contract_sha TEXT NOT NULL, status TEXT NOT NULL, added_offset_cents INTEGER NOT NULL,
  total_offset_cents INTEGER NOT NULL, carry_cents INTEGER NOT NULL, refund_cents INTEGER NOT NULL,
  applied_cents INTEGER NOT NULL, refunded_cents INTEGER NOT NULL, available_cents INTEGER NOT NULL,
  refund_due_cents INTEGER NOT NULL, evidence_sha TEXT NOT NULL, raw BLOB NOT NULL,
  reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(note_id,new_run_id));
CREATE TABLE IF NOT EXISTS credit_audit(
  id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES credit_note(id), as_of TEXT NOT NULL,
  status TEXT NOT NULL, available_cents INTEGER NOT NULL, refund_due_cents INTEGER NOT NULL,
  current_run_id INTEGER, readjudication_id INTEGER REFERENCES credit_readjudication(id),
  reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def _columns(db, table):
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def connect(path):
    db = settlement_connect(path)
    # Readjudication is introduced in a later schema revision; older ledgers
    # still contain credit_contract/credit_audit, so the additive table and
    # columns are applied before the current SCHEMA is declared.
    columns = _columns(db, 'credit_contract')
    if columns and 'settlement_change_policy' not in columns:
        db.execute("ALTER TABLE credit_contract ADD COLUMN settlement_change_policy TEXT NOT NULL "
                   "DEFAULT 'freeze_review'")
    db.executescript(SCHEMA)
    if 'readjudication_id' not in _columns(db, 'credit_audit'):
        db.execute('ALTER TABLE credit_audit ADD COLUMN readjudication_id INTEGER '
                   'REFERENCES credit_readjudication(id)')
    db.commit()
    return db
