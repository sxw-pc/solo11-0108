import csv,hashlib,io,json
from pathlib import Path
from .store import connect
def read_csv(path):
    raw=Path(path).read_bytes();return raw,list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
def _erasure_guards(db,table,column):
    guards={}
    for g in db.execute('SELECT old_key,new_key,field FROM erasure_guard WHERE table_name=?',(table,)):
        key=g[column]
        if key is not None:guards.setdefault(key,set()).add(g['field'])
    return guards
def _check_erasure_guards(guards,key,row):
    erased=guards.get(key)
    if not erased:return
    if '*' in erased:raise ValueError('erased_entity_reimport')
    if any(f in erased and row.get(f) is not None and str(row[f]).strip() for f in erased):raise ValueError('erased_field_reimport')
def import_rows(db_path,table,path):
    db=connect(db_path);raw,rows=read_csv(path);h=hashlib.sha256(raw).hexdigest();count=0
    with db:
        c=db.execute('SELECT * FROM table_contract WHERE name=?',(table,)).fetchone()
        if not c:raise ValueError('unknown_table')
        pk_fields=c['pk'].split('|');natural_fields=c['natural'].split('|')
        ref_fields=[f for ref in json.loads(c['refs']) for f in (ref['field'] if isinstance(ref['field'],list) else [ref['field']])]
        required=pk_fields+natural_fields+ref_fields
        if not rows or any(f not in rows[0] for f in required):raise ValueError('missing_column')
        guards=_erasure_guards(db,table,'old_key')
        for row in rows:
            if any(row.get(f) is None or not str(row[f]).strip() for f in pk_fields):continue
            _check_erasure_guards(guards,'|'.join(row[f] for f in pk_fields),row)
        old=db.execute('SELECT id FROM source_file WHERE sha=?',(h,)).fetchone()
        if old:return 0
        sid=db.execute('INSERT INTO source_file(path,sha,raw) VALUES (?,?,?)',(str(path),h,raw)).lastrowid
        for line_no,row in enumerate(rows,2):
            if any(row.get(f) is None or not str(row[f]).strip() for f in required):raise ValueError('missing_key_value')
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
        guards=_erasure_guards(db,table,'new_key')
        for r in rows:
            if r.get('new_key'):_check_erasure_guards(guards,r['new_key'],r)
        db.execute('INSERT OR IGNORE INTO source_file(path,sha,raw) VALUES (?,?,?)',(str(path),h,raw))
        for r in rows:
            payload=json.dumps(r,sort_keys=True)
            old=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(table,r['new_key'])).fetchone()
            if old:
                if old['payload']!=payload:raise ValueError('target_snapshot_conflict')
                continue
            db.execute('INSERT INTO target_row VALUES (?,?,?)',(table,r['new_key'],payload))
    db.close();return len(rows)
