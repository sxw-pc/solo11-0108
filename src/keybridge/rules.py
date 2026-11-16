import hashlib,json
from .store import connect
from .identity import allocate
def digest_rows(db,table):
    rows=[tuple(x) for x in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
    return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
def plan(path):
    db=connect(path);actions=[]
    with db:
        db.execute('DELETE FROM issue');db.execute('DELETE FROM identity_map');db.execute('DELETE FROM reference_edge')
        allocate(db)
        contracts=[dict(x) for x in db.execute('SELECT * FROM table_contract ORDER BY position')]
        for c in contracts:
            for r in db.execute('SELECT * FROM source_row WHERE table_name=? ORDER BY old_key',(c['name'],)).fetchall():
                mapped=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(c['name'],r['old_key'])).fetchone()
                if not mapped:continue
                payload=json.loads(r['payload']);new=mapped['new_key']
                if '|' in c['pk']:payload['__new_key']=new
                else:payload[c['pk']]=new
                blocked=False
                for ref in json.loads(c['refs']):
                    fields=ref['field'] if isinstance(ref['field'],list) else [ref['field']]
                    parent_old='|'.join(payload[f] for f in fields)
                    parent=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(ref['table'],parent_old)).fetchone()
                    if not parent:
                        db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('orphan_reference',c['name']+':'+r['old_key'],'|'.join(fields)));blocked=True;continue
                    if len(fields)>1:payload['__ref_'+ref['table']+'_new_key']=parent['new_key']
                    else:payload[fields[0]]=parent['new_key']
                    db.execute('INSERT INTO reference_edge(child_table,child_old,parent_table,parent_old,parent_new) VALUES (?,?,?,?,?)',(c['name'],r['old_key'],ref['table'],parent_old,parent['new_key']))
                if blocked:continue
                for existing in db.execute('SELECT new_key,payload FROM target_row WHERE table_name=?',(c['name'],)):
                    old_payload=json.loads(existing['payload'])
                    target_natural='|'.join(str(old_payload.get(f,'')).strip().casefold() for f in c['natural'].split('|'))
                    if target_natural==r['natural'] and existing['new_key']!=new:
                        db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',('natural_target_conflict',c['name']+':'+r['old_key']));blocked=True;break
                if blocked:continue
                target=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(c['name'],new)).fetchone()
                if target:
                    if json.loads(target['payload'])==payload:kind='reuse'
                    else:
                        db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',('target_conflict',c['name']+':'+new));continue
                else:kind='insert'
                actions.append({'table':c['name'],'old':r['old_key'],'new':new,'payload':payload,'kind':kind})
        source_hash=digest_rows(db,'source_row');target_hash=digest_rows(db,'target_row')
        status='blocked' if db.execute('SELECT 1 FROM issue LIMIT 1').fetchone() or db.execute('SELECT 1 FROM collision_group LIMIT 1').fetchone() else 'ready'
        pid=db.execute('INSERT INTO migration_plan(source_hash,target_hash,status,actions) VALUES (?,?,?,?)',(source_hash,target_hash,status,json.dumps(actions,sort_keys=True))).lastrowid
    db.close();return pid,status,actions
def apply(path,plan_id,fail_after=None):
    db=connect(path)
    try:
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p or p['status']!='ready':raise ValueError('plan_not_ready')
            if p['source_hash']!=digest_rows(db,'source_row') or p['target_hash']!=digest_rows(db,'target_row'):raise ValueError('stale_plan')
            for n,a in enumerate(json.loads(p['actions']),1):
                if a['kind']=='insert':
                    db.execute('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',(plan_id,a['table'],a['new']))
                    db.execute('INSERT INTO target_row VALUES (?,?,?)',(a['table'],a['new'],json.dumps(a['payload'],sort_keys=True)))
                db.execute('INSERT INTO apply_step(plan_id,table_name,new_key,action) VALUES (?,?,?,?)',(plan_id,a['table'],a['new'],a['kind']))
                if fail_after==n:raise RuntimeError('injected_failure')
            db.execute("UPDATE migration_plan SET status='applied' WHERE id=?",(plan_id,))
    finally:db.close()
def rollback(path,plan_id):
    db=connect(path)
    with db:
        for r in db.execute('SELECT * FROM rollback_entry WHERE plan_id=? ORDER BY id DESC',(plan_id,)).fetchall():db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(r['table_name'],r['new_key']))
        db.execute('DELETE FROM apply_step WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(plan_id,));db.execute("UPDATE migration_plan SET status='rolled_back' WHERE id=?",(plan_id,))
    db.close()
