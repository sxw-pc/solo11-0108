import hashlib
import json
import sqlite3
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS dataset(id TEXT PRIMARY KEY,contract TEXT NOT NULL,sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS table_contract(name TEXT PRIMARY KEY,pk TEXT,natural TEXT,refs TEXT,position INTEGER);
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,path TEXT,sha TEXT UNIQUE,raw BLOB);
CREATE TABLE IF NOT EXISTS source_row(id INTEGER PRIMARY KEY,table_name TEXT,old_key TEXT,natural TEXT,payload TEXT,source_id INTEGER REFERENCES source_file(id),line_no INTEGER,UNIQUE(table_name,old_key));
CREATE TABLE IF NOT EXISTS identity_map(table_name TEXT,old_key TEXT,new_key TEXT,rule_version TEXT,PRIMARY KEY(table_name,old_key));
CREATE TABLE IF NOT EXISTS reference_edge(id INTEGER PRIMARY KEY,child_table TEXT,child_old TEXT,parent_table TEXT,parent_old TEXT,parent_new TEXT);
CREATE TABLE IF NOT EXISTS collision_group(id INTEGER PRIMARY KEY,table_name TEXT,old_key TEXT,reason TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS target_row(table_name TEXT,new_key TEXT,payload TEXT,PRIMARY KEY(table_name,new_key));
CREATE TABLE IF NOT EXISTS migration_plan(id INTEGER PRIMARY KEY,source_hash TEXT,target_hash TEXT,status TEXT,actions TEXT);
CREATE TABLE IF NOT EXISTS apply_step(id INTEGER PRIMARY KEY,plan_id INTEGER,table_name TEXT,new_key TEXT,action TEXT);
CREATE TABLE IF NOT EXISTS rollback_entry(id INTEGER PRIMARY KEY,plan_id INTEGER,table_name TEXT,new_key TEXT,old_payload TEXT);
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT,subject TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS target_audit(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES migration_plan(id),status TEXT NOT NULL,source_digest TEXT NOT NULL,target_digest TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS target_audit_detail(audit_id INTEGER REFERENCES target_audit(id),table_name TEXT,new_key TEXT,code TEXT,expected TEXT,actual TEXT,PRIMARY KEY(audit_id,table_name,new_key));
CREATE TABLE IF NOT EXISTS target_audit_evidence(
  audit_id INTEGER REFERENCES target_audit(id),
  table_name TEXT NOT NULL,
  new_key TEXT NOT NULL,
  old_key TEXT NOT NULL,
  value_source TEXT NOT NULL,
  correction_revision INTEGER,
  history_id INTEGER,
  recorded_seq INTEGER,
  valid_from TEXT,
  valid_to TEXT,
  recorded_at TEXT,
  valid_cutoff TEXT,
  recorded_cutoff TEXT,
  expected_payload TEXT,
  actual_payload TEXT,
  expected_sha TEXT NOT NULL,
  actual_sha TEXT,
  evidence TEXT,
  verdict TEXT NOT NULL,
  PRIMARY KEY(audit_id,table_name,new_key)
);
CREATE TABLE IF NOT EXISTS client_contract(id TEXT PRIMARY KEY,critical INTEGER NOT NULL,table_name TEXT NOT NULL,config_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lookup_observation(id INTEGER PRIMARY KEY,client_id TEXT REFERENCES client_contract(id),plan_id INTEGER REFERENCES migration_plan(id),sequence INTEGER NOT NULL,old_key TEXT NOT NULL,observed_new_key TEXT,observed_sha TEXT,UNIQUE(client_id,sequence));
CREATE TABLE IF NOT EXISTS retirement_gate(id INTEGER PRIMARY KEY,audit_id INTEGER REFERENCES target_audit(id),status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS retirement_member(gate_id INTEGER REFERENCES retirement_gate(id),client_id TEXT REFERENCES client_contract(id),old_key TEXT,status TEXT,observation_id INTEGER REFERENCES lookup_observation(id),PRIMARY KEY(gate_id,client_id,old_key));
CREATE TABLE IF NOT EXISTS correction_batch(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES migration_plan(id),source_sha TEXT UNIQUE NOT NULL,raw BLOB NOT NULL,status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS correction_item(id INTEGER PRIMARY KEY,batch_id INTEGER REFERENCES correction_batch(id),table_name TEXT NOT NULL,old_key TEXT NOT NULL,expected_revision INTEGER NOT NULL,after_source TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,base_sha TEXT,target_sha TEXT,desired_target TEXT,merged_target TEXT,UNIQUE(batch_id,table_name,old_key));
CREATE TABLE IF NOT EXISTS correction_head(table_name TEXT,old_key TEXT,revision INTEGER NOT NULL,source_payload TEXT NOT NULL,projected_payload TEXT NOT NULL,target_payload TEXT NOT NULL,updated_by INTEGER REFERENCES correction_item(id),PRIMARY KEY(table_name,old_key));
CREATE TABLE IF NOT EXISTS correction_conflict(item_id INTEGER REFERENCES correction_item(id),field TEXT,base TEXT,target TEXT,requested TEXT,PRIMARY KEY(item_id,field));
CREATE TABLE IF NOT EXISTS correction_history(
  id INTEGER PRIMARY KEY,
  table_name TEXT NOT NULL,
  old_key TEXT NOT NULL,
  batch_id INTEGER REFERENCES correction_batch(id),
  item_id INTEGER REFERENCES correction_item(id),
  revision INTEGER NOT NULL,
  source_payload TEXT NOT NULL,
  declared_fields TEXT NOT NULL,
  projected_payload TEXT NOT NULL,
  target_payload TEXT NOT NULL,
  valid_from TEXT,
  valid_to TEXT,
  recorded_at TEXT NOT NULL,
  recorded_seq INTEGER NOT NULL UNIQUE,
  source_sha TEXT NOT NULL,
  content_sha TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS correction_history_identity ON correction_history(table_name,old_key,recorded_seq);
"""
# Additive columns keep databases created by the previous correction workflow usable.
# Every entry is (table, column, type); ALTER TABLE only runs when the column is absent.
MIGRATIONS=[
 ('correction_head','valid_from','TEXT'),
 ('correction_head','valid_to','TEXT'),
 ('correction_head','recorded_at','TEXT'),
 ('correction_head','recorded_seq','INTEGER'),
 ('correction_head','history_id','INTEGER'),
 ('correction_item','valid_from','TEXT'),
 ('correction_item','valid_to','TEXT'),
 ('correction_item','recorded_at','TEXT'),
 ('correction_item','declared_fields','TEXT'),
 ('correction_history','content_sha','TEXT'),
 ('target_audit','valid_cutoff','TEXT'),
 ('target_audit','recorded_cutoff','TEXT'),
]
_TRIGGERS="""
CREATE TRIGGER IF NOT EXISTS correction_history_no_update BEFORE UPDATE ON correction_history
BEGIN SELECT RAISE(ABORT,'correction_history_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS correction_history_no_delete BEFORE DELETE ON correction_history
BEGIN SELECT RAISE(ABORT,'correction_history_is_append_only'); END;
CREATE TRIGGER IF NOT EXISTS target_audit_evidence_no_update BEFORE UPDATE ON target_audit_evidence
BEGIN SELECT RAISE(ABORT,'target_audit_evidence_is_immutable'); END;
CREATE TRIGGER IF NOT EXISTS target_audit_evidence_no_delete BEFORE DELETE ON target_audit_evidence
BEGIN SELECT RAISE(ABORT,'target_audit_evidence_is_immutable'); END;
"""
def _backfill_legacy_history(db):
    """Turn pre-bitemporal ``correction_head`` rows into history observations.

    Older databases only stored the current head. Without a history row those
    corrections would be invisible to valid/record queries, so each head
    lacking ``history_id`` is materialised once:

    * ``recorded_at`` uses the owning batch's receipt timestamp;
    * ``valid_from`` defaults to that receipt time (the only instant we can
      honestly claim the correction was effective);
    * ``declared_fields`` is the diff between the frozen source row and the
      head's legacy source payload.

    The head is then linked to the new history row. This is a one-time
    upgrade, not a rewrite of the correction.
    """
    heads=db.execute(
        'SELECT h.*, c.created_at AS batch_created_at, i.batch_id AS item_batch_id '
        'FROM correction_head h '
        'LEFT JOIN correction_item i ON i.id=h.updated_by '
        'LEFT JOIN correction_batch c ON c.id=i.batch_id '
        'WHERE h.history_id IS NULL ORDER BY h.table_name,h.old_key,h.revision').fetchall()
    if not heads:
        return
    sequence=db.execute('SELECT COALESCE(MAX(recorded_seq),0) FROM correction_history').fetchone()[0]
    for head in heads:
        original_row=db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?',
                                (head['table_name'],head['old_key'])).fetchone()
        original=json.loads(original_row['payload']) if original_row else {}
        after=json.loads(head['source_payload'])
        declared=sorted(field for field in set(original)|set(after)
                        if original.get(field)!=after.get(field))
        recorded_at=head['batch_created_at'] or db.execute('SELECT CURRENT_TIMESTAMP').fetchone()[0]
        valid_from=head['valid_from'] or recorded_at
        sequence+=1
        values={'table_name':head['table_name'],'old_key':head['old_key'],
                'batch_id':head['item_batch_id'],'item_id':head['updated_by'],
                'revision':head['revision'],'source_payload':head['source_payload'],
                'declared_fields':json.dumps(declared,sort_keys=True),
                'projected_payload':head['projected_payload'],'target_payload':head['target_payload'],
                'valid_from':valid_from,'valid_to':head['valid_to'],
                'recorded_at':recorded_at,'recorded_seq':sequence,
                'source_sha':hashlib.sha256(head['source_payload'].encode()).hexdigest()}
        history_id=db.execute(
            'INSERT INTO correction_history(table_name,old_key,batch_id,item_id,revision,source_payload,'
            'declared_fields,projected_payload,target_payload,valid_from,valid_to,recorded_at,recorded_seq,'
            'source_sha,content_sha) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (values['table_name'],values['old_key'],values['batch_id'],values['item_id'],
             values['revision'],values['source_payload'],values['declared_fields'],
             values['projected_payload'],values['target_payload'],values['valid_from'],
             values['valid_to'],values['recorded_at'],values['recorded_seq'],values['source_sha'],
             correction_content_sha(values))).lastrowid
        db.execute('UPDATE correction_head SET history_id=?,valid_from=?,recorded_at=?,recorded_seq=? WHERE table_name=? AND old_key=?',
                   (history_id,values['valid_from'],values['recorded_at'],values['recorded_seq'],
                    head['table_name'],head['old_key']))
def _migrate(db):
    for table,column,kind in MIGRATIONS:
        existing={row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
        if column not in existing:
            db.execute('ALTER TABLE '+table+' ADD COLUMN '+column+' '+kind)
    # Backfill content digests for rows written before digests existed. The
    # append-only trigger must step aside for this one maintenance pass.
    if db.execute('SELECT 1 FROM correction_history WHERE content_sha IS NULL LIMIT 1').fetchone():
        db.execute('DROP TRIGGER IF EXISTS correction_history_no_update')
        for row in db.execute('SELECT * FROM correction_history WHERE content_sha IS NULL').fetchall():
            db.execute('UPDATE correction_history SET content_sha=? WHERE id=?',(correction_content_sha(row),row['id']))
    _backfill_legacy_history(db)
    db.executescript(_TRIGGERS)
def correction_content_sha(values):
    """SHA over the semantic content of a correction-history row.

    Covers every column except the surrogate key and creation timestamp, so a
    rewrite of valid/record time or payload is detectable.
    """
    payload={key:value for key,value in dict(values).items() if key not in {'id','created_at','content_sha'}}
    return hashlib.sha256(json.dumps(payload,sort_keys=True,default=str).encode()).hexdigest()
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    _migrate(db);db.commit();return db
