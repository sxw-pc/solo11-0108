"""Independent invoice/payment/reversal evidence ledger."""
from .store import connect as base_connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS settlement_file(id INTEGER PRIMARY KEY,sha TEXT NOT NULL UNIQUE,raw BLOB NOT NULL,received_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settlement_event(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,sha TEXT NOT NULL,raw BLOB NOT NULL,business_time TEXT,received_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settlement_invoice(id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES settlement_event(event_id),party TEXT NOT NULL,reference TEXT NOT NULL,currency TEXT NOT NULL,cents INTEGER NOT NULL,issued_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_payment(id TEXT PRIMARY KEY,event_id TEXT NOT NULL REFERENCES settlement_event(event_id),party TEXT NOT NULL,reference TEXT NOT NULL,currency TEXT NOT NULL,cents INTEGER NOT NULL,paid_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_claim(event_id TEXT PRIMARY KEY REFERENCES settlement_event(event_id),payment_id TEXT NOT NULL REFERENCES settlement_payment(id),invoice_id TEXT NOT NULL REFERENCES settlement_invoice(id),cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_reversal(event_id TEXT PRIMARY KEY REFERENCES settlement_event(event_id),payment_id TEXT NOT NULL REFERENCES settlement_payment(id),invoice_id TEXT, cents INTEGER NOT NULL,reversed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settlement_run(id INTEGER PRIMARY KEY,source_sha TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS settlement_edge(run_id INTEGER NOT NULL REFERENCES settlement_run(id),payment_id TEXT NOT NULL,invoice_id TEXT NOT NULL,cents INTEGER NOT NULL,source TEXT NOT NULL,PRIMARY KEY(run_id,payment_id,invoice_id,source));
CREATE TABLE IF NOT EXISTS settlement_invoice_decision(run_id INTEGER NOT NULL REFERENCES settlement_run(id),invoice_id TEXT NOT NULL,state TEXT NOT NULL,paid_cents INTEGER NOT NULL,outstanding_cents INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(run_id,invoice_id));
CREATE TABLE IF NOT EXISTS settlement_payment_decision(run_id INTEGER NOT NULL REFERENCES settlement_run(id),payment_id TEXT NOT NULL,state TEXT NOT NULL,net_cents INTEGER NOT NULL,allocated_cents INTEGER NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(run_id,payment_id));
CREATE TABLE IF NOT EXISTS settlement_issue(run_id INTEGER NOT NULL REFERENCES settlement_run(id),subject TEXT NOT NULL,code TEXT NOT NULL,detail TEXT NOT NULL,PRIMARY KEY(run_id,subject,code));
CREATE TABLE IF NOT EXISTS settlement_head(id INTEGER PRIMARY KEY CHECK(id=1),run_id INTEGER NOT NULL REFERENCES settlement_run(id));
"""
def connect(path):
    db=base_connect(path)
    db.executescript(SCHEMA)
    return db
