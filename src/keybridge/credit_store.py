"""Customer credit obligations, separate from payment matching and migration."""

from .settlement_store import connect as settlement_connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_contract(
  id TEXT PRIMARY KEY, party TEXT NOT NULL, currency TEXT NOT NULL, disposition TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS credit_audit(
  id INTEGER PRIMARY KEY, note_id TEXT NOT NULL REFERENCES credit_note(id), as_of TEXT NOT NULL,
  status TEXT NOT NULL, available_cents INTEGER NOT NULL, refund_due_cents INTEGER NOT NULL,
  current_run_id INTEGER, reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
"""


def connect(path):
    db = settlement_connect(path)
    db.executescript(SCHEMA)
    return db
