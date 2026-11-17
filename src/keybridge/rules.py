import hashlib,json
from pathlib import Path
from .store import connect
from .identity import allocate
from .attachments import ensure_published,journal as attachment_journal
def digest_rows(db,table):
    rows=[tuple(x) for x in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
    return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
def _attachment_refs(entries,payload):
    atts=[]
    for m in entries:
        ref={'$cas':m['sha'],'name':Path(m['path']).name,'size':m['size']}
        if m['field'] in payload:ref['original']=payload[m['field']]
        payload[m['field']]=ref
        atts.append({'field':m['field'],'sha':m['sha'],'size':m['size']})
    return atts
def _verify_manifest_current(db,actions):
    for a in actions:
        expected={m['field']:m['sha'] for m in db.execute('SELECT field,sha FROM attachment_manifest WHERE table_name=? AND old_key=?',(a['table'],a['old']))}
        actual={att['field']:att['sha'] for att in a.get('attachments',[])}
        if expected!=actual:raise ValueError('stale_plan')
def plan(path):
    db=connect(path);actions=[]
    with db:
        db.execute('DELETE FROM issue');db.execute('DELETE FROM identity_map');db.execute('DELETE FROM reference_edge')
        allocate(db)
        contracts=[dict(x) for x in db.execute('SELECT * FROM table_contract ORDER BY position')]
        manifest={}
        for m in db.execute('SELECT * FROM attachment_manifest ORDER BY field'):
            manifest.setdefault((m['table_name'],m['old_key']),[]).append(m)
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
                atts=_attachment_refs(manifest.get((c['name'],r['old_key']),[]),payload)
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
                action={'table':c['name'],'old':r['old_key'],'new':new,'payload':payload,'kind':kind}
                if atts:action['attachments']=atts
                actions.append(action)
        source_hash=digest_rows(db,'source_row');target_hash=digest_rows(db,'target_row')
        status='blocked' if db.execute('SELECT 1 FROM issue LIMIT 1').fetchone() or db.execute('SELECT 1 FROM collision_group LIMIT 1').fetchone() else 'ready'
        pid=db.execute('INSERT INTO migration_plan(source_hash,target_hash,status,actions) VALUES (?,?,?,?)',(source_hash,target_hash,status,json.dumps(actions,sort_keys=True))).lastrowid
    db.close();return pid,status,actions
def apply(path,plan_id,fail_after=None,cas_dir=None,fail_at=None):
    db=connect(path)
    try:
        p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
        if not p or p['status']!='ready':raise ValueError('plan_not_ready')
        if p['source_hash']!=digest_rows(db,'source_row') or p['target_hash']!=digest_rows(db,'target_row'):raise ValueError('stale_plan')
        actions=json.loads(p['actions'])
        _verify_manifest_current(db,actions)
        for a in actions:
            for att in a.get('attachments',[]):
                ensure_published(path,att['sha'],att['size'],cas_dir)
        if fail_at=='pre_commit':raise RuntimeError('injected_failure')
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p or p['status']!='ready':raise ValueError('plan_not_ready')
            if p['source_hash']!=digest_rows(db,'source_row') or p['target_hash']!=digest_rows(db,'target_row'):raise ValueError('stale_plan')
            for n,a in enumerate(actions,1):
                if a['kind']=='insert':
                    db.execute('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',(plan_id,a['table'],a['new']))
                    db.execute('INSERT INTO target_row VALUES (?,?,?)',(a['table'],a['new'],json.dumps(a['payload'],sort_keys=True)))
                    for att in a.get('attachments',[]):
                        db.execute('INSERT INTO attachment_ref(plan_id,table_name,new_key,field,sha) VALUES (?,?,?,?,?)',(plan_id,a['table'],a['new'],att['field'],att['sha']))
                db.execute('INSERT INTO apply_step(plan_id,table_name,new_key,action) VALUES (?,?,?,?)',(plan_id,a['table'],a['new'],a['kind']))
                if fail_after==n:raise RuntimeError('injected_failure')
            refs=[{'table':a['table'],'new':a['new'],'field':att['field'],'sha':att['sha']} for a in actions if a['kind']=='insert' for att in a.get('attachments',[])]
            if refs:attachment_journal(db,'ref_commit',plan_id=plan_id,detail=json.dumps(refs,sort_keys=True))
            db.execute("UPDATE migration_plan SET status='applied' WHERE id=?",(plan_id,))
    finally:db.close()
def rollback(path,plan_id,fail_after=None):
    db=connect(path)
    with db:
        p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
        if not p or p['status']!='applied':raise ValueError('plan_not_applied')
        actions=json.loads(p['actions'])
        steps=db.execute('SELECT table_name,new_key,action FROM apply_step WHERE plan_id=? ORDER BY id',(plan_id,)).fetchall()
        if len(steps)!=len(actions) or any(tuple(step)!=(a['table'],a['new'],a['kind']) for step,a in zip(steps,actions)):
            raise ValueError('apply_ledger_changed')
        entries=db.execute('SELECT table_name,new_key FROM rollback_entry WHERE plan_id=?',(plan_id,)).fetchall()
        owned={(r['table_name'],r['new_key']) for r in entries}
        expected={(a['table'],a['new']) for a in actions if a['kind']=='insert'}
        if owned!=expected or len(entries)!=len(expected):raise ValueError('rollback_ledger_changed')
        expected_refs={(a['table'],a['new'],att['field'],att['sha']) for a in actions if a['kind']=='insert' for att in a.get('attachments',[])}
        actual_refs={(r['table_name'],r['new_key'],r['field'],r['sha']) for r in db.execute('SELECT table_name,new_key,field,sha FROM attachment_ref WHERE plan_id=?',(plan_id,))}
        if actual_refs!=expected_refs:raise ValueError('rollback_ledger_changed')
        for action in actions:
            if action['kind']!='insert':continue
            current=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(action['table'],action['new'])).fetchone()
            if not current or json.loads(current['payload'])!=action['payload']:
                raise ValueError('rollback_conflict')
        for contract in db.execute('SELECT name,refs FROM table_contract'):
            for row in db.execute('SELECT new_key,payload FROM target_row WHERE table_name=?',(contract['name'],)):
                if (contract['name'],row['new_key']) in owned:continue
                payload=json.loads(row['payload'])
                for ref in json.loads(contract['refs']):
                    fields=ref['field'] if isinstance(ref['field'],list) else [ref['field']]
                    field=fields[0] if len(fields)==1 else '__ref_'+ref['table']+'_new_key'
                    if (ref['table'],str(payload.get(field))) in owned:
                        raise ValueError('rollback_referenced_by_external_row')
        n=0
        for r in db.execute('SELECT * FROM rollback_entry WHERE plan_id=? ORDER BY id DESC',(plan_id,)).fetchall():
            db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(r['table_name'],r['new_key']))
            n+=1
            if fail_after==n:raise RuntimeError('injected_failure')
        db.execute('DELETE FROM attachment_ref WHERE plan_id=?',(plan_id,))
        if expected_refs:attachment_journal(db,'ref_release',plan_id=plan_id,detail=json.dumps(sorted(expected_refs)))
        db.execute('DELETE FROM apply_step WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(plan_id,));db.execute("UPDATE migration_plan SET status='rolled_back' WHERE id=?",(plan_id,))
    db.close()
