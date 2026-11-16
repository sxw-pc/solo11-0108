import csv,hashlib,io,json
from pathlib import Path
from .store import connect
def read_csv(path):
    raw=Path(path).read_bytes();return raw,list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
def import_rows(db_path,table,path):
    db=connect(db_path);raw,rows=read_csv(path);h=hashlib.sha256(raw).hexdigest();count=0
    with db:
        c=db.execute('SELECT * FROM table_contract WHERE name=?',(table,)).fetchone()
        if not c:raise ValueError('unknown_table')
        pk_fields=c['pk'].split('|');natural_fields=c['natural'].split('|')
        if not rows or any(f not in rows[0] for f in pk_fields+natural_fields):raise ValueError('missing_column')
        old=db.execute('SELECT id FROM source_file WHERE sha=?',(h,)).fetchone()
        if old:return 0
        sid=db.execute('INSERT INTO source_file(path,sha,raw) VALUES (?,?,?)',(str(path),h,raw)).lastrowid
        for line_no,row in enumerate(rows,2):
            key='|'.join(row[f] for f in pk_fields);natural='|'.join(row[f].strip().casefold() for f in natural_fields);payload=json.dumps(row,sort_keys=True)
            existing=db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?',(table,key)).fetchone()
            if existing:
                if existing['payload']!=payload:
                    db.execute('INSERT INTO collision_group(table_name,old_key,reason,source_id) VALUES (?,?,?,?)',(table,key,'old_key_conflict',sid))
                continue
            db.execute('INSERT INTO source_row(table_name,old_key,natural,payload,source_id,line_no) VALUES (?,?,?,?,?,?)',(table,key,natural,payload,sid,line_no));count+=1
    db.close();return count
def import_target(db_path,table,path):
    db=connect(db_path);raw,rows=read_csv(path)
    with db:
        h=hashlib.sha256(raw).hexdigest()
        db.execute('INSERT OR IGNORE INTO source_file(path,sha,raw) VALUES (?,?,?)',(str(path),h,raw))
        for r in rows:db.execute('INSERT OR REPLACE INTO target_row VALUES (?,?,?)',(table,r['new_key'],json.dumps(r,sort_keys=True)))
    db.close();return len(rows)
