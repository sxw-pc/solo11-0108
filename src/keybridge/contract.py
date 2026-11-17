import hashlib,json
from .store import connect
from .timeline import topological
def digest(v):return hashlib.sha256(v.encode() if isinstance(v,str) else v).hexdigest()
def _fields(value):return value if isinstance(value,list) else [value]
def compile_contract(path,db_path):
    raw=open(path,'rb').read();obj=json.loads(raw);tables=obj['tables'];names=[t['name'] for t in tables]
    if len(names)!=len(set(names)):raise ValueError('duplicate_table')
    graph={t['name']:{r['table'] for r in t.get('refs',[])} for t in tables}
    if any(p not in graph for parents in graph.values() for p in parents):raise ValueError('unknown_reference')
    order=topological(graph)
    db=connect(db_path)
    with db:
        old=db.execute('SELECT sha FROM dataset WHERE id=?',(obj.get('id','demo'),)).fetchone()
        if old and old['sha']!=digest(raw):raise ValueError('contract_changed')
        db.execute('INSERT OR IGNORE INTO dataset VALUES (?,?,?)',(obj.get('id','demo'),raw.decode(),digest(raw)))
        db.execute('DELETE FROM table_sensitive')
        for n,t in enumerate(tables):
            pk=_fields(t['pk']);natural=_fields(t['natural'])
            ref_fields=[f for ref in t.get('refs',[]) for f in _fields(ref['field'])]
            sensitive=t.get('sensitive',[])
            if not isinstance(sensitive,list) or any(not isinstance(f,str) or not f for f in sensitive):raise ValueError('invalid_sensitive_field')
            if len(set(sensitive))!=len(sensitive):raise ValueError('duplicate_sensitive_field')
            if any(f in set(pk)|set(natural)|set(ref_fields) for f in sensitive):raise ValueError('sensitive_key_field')
            db.execute('INSERT OR REPLACE INTO table_contract VALUES (?,?,?,?,?)',(t['name'],'|'.join(pk),'|'.join(natural),json.dumps(t.get('refs',[])),order.index(t['name'])))
            for f in sensitive:db.execute('INSERT INTO table_sensitive VALUES (?,?)',(t['name'],f))
    db.close();return order
