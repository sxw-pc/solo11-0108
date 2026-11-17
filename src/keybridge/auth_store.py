"""Separate durable authorization, access and credential records."""
from .store import connect

SCHEMA="""
CREATE TABLE IF NOT EXISTS auth_batch(
 id INTEGER PRIMARY KEY, source_sha TEXT UNIQUE NOT NULL, raw BLOB NOT NULL,
 created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS auth_event(
 grant_id TEXT NOT NULL, revision INTEGER NOT NULL, batch_id INTEGER NOT NULL REFERENCES auth_batch(id),
 table_name TEXT NOT NULL, old_key TEXT NOT NULL, principal TEXT NOT NULL, purpose TEXT NOT NULL,
 fields TEXT NOT NULL, valid_from TEXT NOT NULL, valid_until TEXT NOT NULL,
 effective_at TEXT NOT NULL, action TEXT NOT NULL, event_sha TEXT NOT NULL,
 PRIMARY KEY(grant_id,revision));
CREATE TABLE IF NOT EXISTS auth_head(
 grant_id TEXT PRIMARY KEY, table_name TEXT NOT NULL, old_key TEXT NOT NULL,
 principal TEXT NOT NULL, purpose TEXT NOT NULL, revision INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','revoked','conflict')));
CREATE TABLE IF NOT EXISTS auth_conflict(
 id INTEGER PRIMARY KEY, grant_id TEXT NOT NULL, revision INTEGER NOT NULL,
 event_sha TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_decision(
 request_id TEXT PRIMARY KEY, request_sha TEXT NOT NULL, request_json TEXT NOT NULL,
 decision TEXT NOT NULL CHECK(decision IN ('allow','deny','uncertain')),
 reason TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('current','stale')),
 grant_id TEXT, grant_revision INTEGER, plan_id INTEGER, target_sha TEXT,
 audit_id INTEGER, old_key TEXT, new_key TEXT);
CREATE TABLE IF NOT EXISTS issued_credential(
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES access_decision(request_id),
 grant_id TEXT NOT NULL, grant_revision INTEGER NOT NULL, plan_id INTEGER NOT NULL,
 target_sha TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 token_sha TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('issued','needs_recheck','valid','revoked','stale','expired')));
CREATE TABLE IF NOT EXISTS credential_check(
 id INTEGER PRIMARY KEY, credential_id TEXT NOT NULL REFERENCES issued_credential(id),
 checked_at TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
 current_grant_revision INTEGER, current_target_sha TEXT);
"""

def connect_auth(path):
    db=connect(path)
    db.executescript(SCHEMA)
    return db
