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
CREATE TABLE IF NOT EXISTS client_contract(id TEXT PRIMARY KEY,critical INTEGER NOT NULL,table_name TEXT NOT NULL,config_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lookup_observation(id INTEGER PRIMARY KEY,client_id TEXT REFERENCES client_contract(id),plan_id INTEGER REFERENCES migration_plan(id),sequence INTEGER NOT NULL,old_key TEXT NOT NULL,observed_new_key TEXT,observed_sha TEXT,UNIQUE(client_id,sequence));
CREATE TABLE IF NOT EXISTS retirement_gate(id INTEGER PRIMARY KEY,audit_id INTEGER REFERENCES target_audit(id),status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS retirement_member(gate_id INTEGER REFERENCES retirement_gate(id),client_id TEXT REFERENCES client_contract(id),old_key TEXT,status TEXT,observation_id INTEGER REFERENCES lookup_observation(id),PRIMARY KEY(gate_id,client_id,old_key));
CREATE TABLE IF NOT EXISTS correction_batch(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES migration_plan(id),source_sha TEXT UNIQUE NOT NULL,raw BLOB NOT NULL,status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS correction_item(id INTEGER PRIMARY KEY,batch_id INTEGER REFERENCES correction_batch(id),table_name TEXT NOT NULL,old_key TEXT NOT NULL,expected_revision INTEGER NOT NULL,after_source TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,base_sha TEXT,target_sha TEXT,desired_target TEXT,merged_target TEXT,UNIQUE(batch_id,table_name,old_key));
CREATE TABLE IF NOT EXISTS correction_head(table_name TEXT,old_key TEXT,revision INTEGER NOT NULL,source_payload TEXT NOT NULL,projected_payload TEXT NOT NULL,target_payload TEXT NOT NULL,updated_by INTEGER REFERENCES correction_item(id),PRIMARY KEY(table_name,old_key));
CREATE TABLE IF NOT EXISTS correction_conflict(item_id INTEGER REFERENCES correction_item(id),field TEXT,base TEXT,target TEXT,requested TEXT,PRIMARY KEY(item_id,field));
CREATE TABLE IF NOT EXISTS archive_contract(id TEXT PRIMARY KEY,budget INTEGER NOT NULL,min_per_cohort INTEGER NOT NULL,replica_quorum INTEGER NOT NULL,revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS archive_candidate(id TEXT PRIMARY KEY,contract_id TEXT REFERENCES archive_contract(id),cohort TEXT NOT NULL,units INTEGER NOT NULL,value INTEGER NOT NULL,digest TEXT NOT NULL,legal_hold INTEGER NOT NULL,source_sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS archive_plan(id INTEGER PRIMARY KEY,contract_id TEXT REFERENCES archive_contract(id),revision INTEGER NOT NULL,status TEXT NOT NULL,used_units INTEGER NOT NULL,total_value INTEGER NOT NULL,reason TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS archive_plan_item(plan_id INTEGER REFERENCES archive_plan(id),candidate_id TEXT REFERENCES archive_candidate(id),decision TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(plan_id,candidate_id));
CREATE TABLE IF NOT EXISTS archive_replica_proof(id TEXT PRIMARY KEY,plan_id INTEGER REFERENCES archive_plan(id),candidate_id TEXT REFERENCES archive_candidate(id),domain TEXT NOT NULL,digest TEXT NOT NULL,source_sha TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS archive_audit(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES archive_plan(id),status TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS archive_audit_item(audit_id INTEGER REFERENCES archive_audit(id),candidate_id TEXT REFERENCES archive_candidate(id),status TEXT NOT NULL,domains TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(audit_id,candidate_id));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
