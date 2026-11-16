import hashlib,json
from .store import connect
from .timeline import topological
def digest(v):return hashlib.sha256(v.encode() if isinstance(v,str) else v).hexdigest()
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
        for n,t in enumerate(tables):
            pk='|'.join(t['pk']) if isinstance(t['pk'],list) else t['pk']
            natural='|'.join(t['natural']) if isinstance(t['natural'],list) else t['natural']
            db.execute('INSERT OR REPLACE INTO table_contract VALUES (?,?,?,?,?)',(t['name'],pk,natural,json.dumps(t.get('refs',[])),order.index(t['name'])))
    db.close();return order
