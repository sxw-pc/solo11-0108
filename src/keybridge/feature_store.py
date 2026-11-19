"""Independent point-in-time feature evidence ledger for training and serving."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS feature_contract(
  id TEXT PRIMARY KEY, raw BLOB NOT NULL, sha TEXT NOT NULL, features_json TEXT NOT NULL,
  min_eligible INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS feature_source(
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, sha TEXT NOT NULL UNIQUE, raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS feature_observation(
  observation_id TEXT NOT NULL, revision INTEGER NOT NULL, entity_id TEXT NOT NULL,
  feature_name TEXT NOT NULL, event_at TEXT NOT NULL, available_at TEXT NOT NULL,
  value_json TEXT, action TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES feature_source(id),
  PRIMARY KEY(observation_id,revision));
CREATE TABLE IF NOT EXISTS feature_label(
  label_id TEXT PRIMARY KEY, entity_id TEXT NOT NULL, prediction_at TEXT NOT NULL,
  outcome_at TEXT NOT NULL, known_at TEXT NOT NULL, outcome_json TEXT NOT NULL,
  source_id INTEGER NOT NULL REFERENCES feature_source(id));
CREATE TABLE IF NOT EXISTS feature_training(
  id INTEGER PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES feature_contract(id),
  freeze_at TEXT NOT NULL, status TEXT NOT NULL, eligible_count INTEGER NOT NULL,
  excluded_count INTEGER NOT NULL, manifest_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feature_training_row(
  build_id INTEGER NOT NULL REFERENCES feature_training(id), label_id TEXT NOT NULL,
  entity_id TEXT NOT NULL, prediction_at TEXT NOT NULL, status TEXT NOT NULL,
  reason TEXT NOT NULL, values_json TEXT NOT NULL, evidence_json TEXT NOT NULL,
  PRIMARY KEY(build_id,label_id));
CREATE TABLE IF NOT EXISTS feature_serving_capture(
  id TEXT PRIMARY KEY, contract_id TEXT NOT NULL REFERENCES feature_contract(id),
  prediction_at TEXT NOT NULL, source_id INTEGER NOT NULL REFERENCES feature_source(id));
CREATE TABLE IF NOT EXISTS feature_serving_value(
  capture_id TEXT NOT NULL REFERENCES feature_serving_capture(id), entity_id TEXT NOT NULL,
  feature_name TEXT NOT NULL, value_json TEXT, observation_id TEXT, revision INTEGER,
  PRIMARY KEY(capture_id,entity_id,feature_name));
CREATE TABLE IF NOT EXISTS feature_parity(
  id INTEGER PRIMARY KEY, build_id INTEGER NOT NULL REFERENCES feature_training(id),
  capture_id TEXT NOT NULL REFERENCES feature_serving_capture(id), status TEXT NOT NULL,
  mismatch_json TEXT NOT NULL, checked_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS feature_release(
  id INTEGER PRIMARY KEY, build_id INTEGER NOT NULL UNIQUE REFERENCES feature_training(id),
  parity_id INTEGER NOT NULL REFERENCES feature_parity(id), manifest_sha TEXT NOT NULL,
  released_at TEXT NOT NULL);
"""


def timestamp(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('timestamp_must_be_utc_z')
    try:
        dt = datetime.fromisoformat(value[:-1] + '+00:00')
    except ValueError as exc:
        raise ValueError('invalid_timestamp') from exc
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


@contextmanager
def connect(path):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(dest)
    db.row_factory = sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:
        db.close()


def source(db, kind, file):
    raw = Path(file).read_bytes()
    digest = sha(raw)
    old = db.execute('SELECT id,kind FROM feature_source WHERE sha=?', (digest,)).fetchone()
    if old:
        if old['kind'] != kind:
            raise ValueError('source_kind_conflict')
        return old['id'], json.loads(raw)
    doc = json.loads(raw)
    cur = db.execute('INSERT INTO feature_source(kind,sha,raw) VALUES(?,?,?)', (kind,digest,raw))
    return cur.lastrowid, doc


def rows(db, sql, params=()):
    return [dict(row) for row in db.execute(sql,params)]
