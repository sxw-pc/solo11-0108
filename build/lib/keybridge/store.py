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
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
