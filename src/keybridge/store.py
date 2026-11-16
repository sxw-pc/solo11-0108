import sqlite3
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS dataset(id TEXT PRIMARY KEY,contract TEXT NOT NULL,sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS table_contract(name TEXT PRIMARY KEY,pk TEXT,natural TEXT,refs TEXT,position INTEGER);
CREATE TABLE IF NOT EXISTS source_file(id INTEGER PRIMARY KEY,path TEXT,sha TEXT UNIQUE,raw BLOB);
CREATE TABLE IF NOT EXISTS source_row(id INTEGER PRIMARY KEY,table_name TEXT,old_key TEXT,natural TEXT,payload TEXT,source_id INTEGER REFERENCES source_file(id),line_no INTEGER,UNIQUE(table_name,old_key));
CREATE TABLE IF NOT EXISTS identity_map(table_name TEXT,old_key TEXT,new_key TEXT,rule_version TEXT,PRIMARY KEY(table_name,old_key));
CREATE TABLE IF NOT EXISTS reference_edge(id INTEGER PRIMARY KEY,child_table TEXT,child_old TEXT,parent_table TEXT,parent_old TEXT,parent_new TEXT,field TEXT,group_id INTEGER,target_deferrable INTEGER,target_nullable INTEGER,cyclic INTEGER,backfill INTEGER);
CREATE TABLE IF NOT EXISTS collision_group(id INTEGER PRIMARY KEY,table_name TEXT,old_key TEXT,reason TEXT,source_id INTEGER);
CREATE TABLE IF NOT EXISTS target_row(table_name TEXT,new_key TEXT,payload TEXT,PRIMARY KEY(table_name,new_key));
CREATE TABLE IF NOT EXISTS migration_plan(id INTEGER PRIMARY KEY,source_hash TEXT,target_hash TEXT,status TEXT,actions TEXT,contract_hash TEXT,groups TEXT);
CREATE TABLE IF NOT EXISTS apply_step(id INTEGER PRIMARY KEY,plan_id INTEGER,table_name TEXT,new_key TEXT,action TEXT);
CREATE TABLE IF NOT EXISTS rollback_entry(id INTEGER PRIMARY KEY,plan_id INTEGER,table_name TEXT,new_key TEXT,old_payload TEXT);
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT,subject TEXT,detail TEXT);
CREATE TABLE IF NOT EXISTS target_audit(id INTEGER PRIMARY KEY,plan_id INTEGER REFERENCES migration_plan(id),status TEXT NOT NULL,source_digest TEXT NOT NULL,target_digest TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS target_audit_detail(audit_id INTEGER REFERENCES target_audit(id),table_name TEXT,new_key TEXT,code TEXT,expected TEXT,actual TEXT,PRIMARY KEY(audit_id,table_name,new_key));
CREATE TABLE IF NOT EXISTS client_contract(id TEXT PRIMARY KEY,critical INTEGER NOT NULL,table_name TEXT NOT NULL,config_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lookup_observation(id INTEGER PRIMARY KEY,client_id TEXT REFERENCES client_contract(id),plan_id INTEGER REFERENCES migration_plan(id),sequence INTEGER NOT NULL,old_key TEXT NOT NULL,observed_new_key TEXT,observed_sha TEXT,UNIQUE(client_id,sequence));
CREATE TABLE IF NOT EXISTS retirement_gate(id INTEGER PRIMARY KEY,audit_id INTEGER REFERENCES target_audit(id),status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS retirement_member(gate_id INTEGER REFERENCES retirement_gate(id),client_id TEXT REFERENCES client_contract(id),old_key TEXT,status TEXT,observation_id INTEGER REFERENCES lookup_observation(id),PRIMARY KEY(gate_id,client_id,old_key));
"""
_ADDED_COLUMNS={
 'migration_plan':[('contract_hash','TEXT'),('groups','TEXT')],
 'reference_edge':[('field','TEXT'),('group_id','INTEGER'),('target_deferrable','INTEGER'),
                   ('target_nullable','INTEGER'),('cyclic','INTEGER'),('backfill','INTEGER')],
}
def _upgrade(db):
    for table,columns in _ADDED_COLUMNS.items():
        have={row[1] for row in db.execute('PRAGMA table_info('+table+')')}
        for name,decl in columns:
            if name not in have:db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+decl)
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);_upgrade(db);return db
