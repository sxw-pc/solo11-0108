import hashlib,json
from .store import connect
from .identity import allocate
from .timeline import scc_order,cyclic_component
def digest_rows(db,table):
    rows=[tuple(x) for x in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
    return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
def contract_digest(db):
    contracts=[tuple(x) for x in db.execute('SELECT name,pk,natural,refs,position FROM table_contract ORDER BY name')]
    datasets=[tuple(x) for x in db.execute('SELECT id,sha FROM dataset ORDER BY id')]
    return hashlib.sha256(json.dumps([contracts,datasets],default=str,sort_keys=True).encode()).hexdigest()
def _fields(ref):
    return ref['field'] if isinstance(ref['field'],list) else [ref['field']]
def _storage_field(ref):
    fields=_fields(ref)
    return fields[0] if len(fields)==1 else '__ref_'+ref['table']+'_new_key'
def _old_parent(payload,ref):return '|'.join(payload[f] for f in _fields(ref))
def _new_payload(c,new,payload):
    if '|' in c['pk']:payload['__new_key']=new
    else:payload[c['pk']]=new
    return payload
def _natural_conflict(db,c,r,new):
    for existing in db.execute('SELECT new_key,payload FROM target_row WHERE table_name=?',(c['name'],)):
        old_payload=json.loads(existing['payload'])
        target_natural='|'.join(str(old_payload.get(f,'')).strip().casefold() for f in c['natural'].split('|'))
        if target_natural==r['natural'] and existing['new_key']!=new:return True
    return False
def _edge(db,child_table,child_old,ref,field,parent_old,parent_new,group_id,cyclic,backfill):
    db.execute('INSERT INTO reference_edge(child_table,child_old,parent_table,parent_old,parent_new,field,group_id,target_deferrable,target_nullable,cyclic,backfill) '
               'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               (child_table,child_old,ref['table'],parent_old,parent_new,field,group_id,
                ref.get('deferrable'),ref.get('nullable'),int(cyclic),int(backfill)))
def _cyclic_groups(contracts):
    graph={c['name']:{ref['table'] for ref in json.loads(c['refs'])} for c in contracts}
    by_name={c['name']:c for c in contracts};groups=[];by_table={}
    for gid,component in enumerate(scc_order(graph),1):
        if not cyclic_component(component,graph):continue
        members=set(component);edges=[]
        for name in component:
            for ref in json.loads(by_name[name]['refs']):
                if ref['table'] in members:
                    edges.append({'child_table':name,'field':ref['field'],'parent_table':ref['table'],
                                  'deferrable':ref.get('deferrable'),'nullable':ref.get('nullable')})
        if edges and all(e['deferrable'] is True for e in edges):strategy='deferred'
        elif edges and all(e['nullable'] is True for e in edges):strategy='nullable_staging'
        else:strategy='blocked'
        group={'id':gid,'members':sorted(component),'cyclic':True,'strategy':strategy,
               'edges':edges,'backfills':[]}
        groups.append(group)
        for name in component:by_table[name]=group
    return groups,by_table
def _plan_cyclic_group(db,c_contracts,group,actions):
    members=set(group['members']);records=[];invalid=False
    for name in group['members']:
        c=c_contracts[name];refs=json.loads(c['refs'])
        for r in db.execute('SELECT * FROM source_row WHERE table_name=? ORDER BY old_key',(name,)).fetchall():
            mapped=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(name,r['old_key'])).fetchone()
            if not mapped:invalid=True;continue
            payload=_new_payload(c,mapped['new_key'],json.loads(r['payload']))
            records.append({'c':c,'r':r,'new':mapped['new_key'],'payload':payload,'refs':refs,'resolved':{}})
    for rec in records:
        for ri,ref in enumerate(rec['refs']):
            parent_old=_old_parent(rec['payload'],ref)
            parent=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(ref['table'],parent_old)).fetchone()
            if not parent:
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',
                           ('orphan_reference',rec['c']['name']+':'+rec['r']['old_key'],'|'.join(_fields(ref))))
                invalid=True;continue
            field=_storage_field(ref);internal=ref['table'] in members
            rec['resolved'][ri]=(parent['new_key'],field)
            _edge(db,rec['c']['name'],rec['r']['old_key'],ref,field,parent_old,parent['new_key'],group['id'],
                  internal,internal and group['strategy']=='nullable_staging')
            if internal:
                entry={'child_table':rec['c']['name'],'child_old':rec['r']['old_key'],'child_new':rec['new'],
                       'field':ref['field'],'storage_field':field,'parent_table':ref['table'],
                       'parent_old':parent_old,'parent_new':parent['new_key'],
                       'deferrable':ref.get('deferrable'),'nullable':ref.get('nullable')}
                if group['strategy']=='nullable_staging':group['backfills'].append(entry)
                for edge in group['edges']:
                    if edge['child_table']==entry['child_table'] and edge['field']==entry['field'] and edge['parent_table']==entry['parent_table']:
                        edge.update({'child_old':entry['child_old'],'child_new':entry['child_new'],
                                     'parent_old':entry['parent_old'],'parent_new':entry['parent_new'],
                                     'storage_field':field})
    if group['strategy']=='blocked':
        db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',
                   ('cycle_not_applyable',','.join(group['members']),'non_deferrable_non_nullable'))
        return
    if invalid:return
    for rec in records:
        for ri,(parent_new,field) in rec['resolved'].items():rec['payload'][field]=parent_new
    for rec in records:
        if _natural_conflict(db,rec['c'],rec['r'],rec['new']):
            db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',
                       ('natural_target_conflict',rec['c']['name']+':'+rec['r']['old_key']));invalid=True;continue
        target=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(rec['c']['name'],rec['new'])).fetchone()
        if target:
            if json.loads(target['payload'])==rec['payload']:rec['kind']='reuse'
            else:
                db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',
                           ('target_conflict',rec['c']['name']+':'+rec['new']));invalid=True;continue
        else:rec['kind']='insert'
    if invalid:return
    for rec in records:
        action={'table':rec['c']['name'],'old':rec['r']['old_key'],'new':rec['new'],
                'payload':rec['payload'],'kind':rec['kind'],'group_id':group['id'],'strategy':group['strategy']}
        if group['strategy']=='nullable_staging':
            staged=dict(rec['payload'])
            for ri,(parent_new,field) in rec['resolved'].items():
                if rec['refs'][ri]['table'] in members:staged[field]=None
            action['staged_payload']=staged
        actions.append(action)
def _plan_table(db,c,actions):
    for r in db.execute('SELECT * FROM source_row WHERE table_name=? ORDER BY old_key',(c['name'],)).fetchall():
        mapped=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(c['name'],r['old_key'])).fetchone()
        if not mapped:continue
        payload=json.loads(r['payload']);new=mapped['new_key'];_new_payload(c,new,payload)
        blocked=False
        for ref in json.loads(c['refs']):
            parent_old=_old_parent(payload,ref)
            parent=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(ref['table'],parent_old)).fetchone()
            if not parent:
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',
                           ('orphan_reference',c['name']+':'+r['old_key'],'|'.join(_fields(ref))));blocked=True;continue
            field=_storage_field(ref);payload[field]=parent['new_key']
            _edge(db,c['name'],r['old_key'],ref,field,parent_old,parent['new_key'],None,False,False)
        if blocked:continue
        if _natural_conflict(db,c,r,new):
            db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',('natural_target_conflict',c['name']+':'+r['old_key']));continue
        target=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(c['name'],new)).fetchone()
        if target:
            if json.loads(target['payload'])==payload:kind='reuse'
            else:
                db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',('target_conflict',c['name']+':'+new));continue
        else:kind='insert'
        actions.append({'table':c['name'],'old':r['old_key'],'new':new,'payload':payload,'kind':kind})
def plan(path):
    db=connect(path);actions=[]
    with db:
        db.execute('DELETE FROM issue');db.execute('DELETE FROM identity_map');db.execute('DELETE FROM reference_edge')
        allocate(db)
        contracts=[dict(x) for x in db.execute('SELECT * FROM table_contract ORDER BY position')]
        c_contracts={c['name']:c for c in contracts}
        groups,by_table=_cyclic_groups(contracts);done=set()
        for c in contracts:
            name=c['name']
            if name in done:continue
            group=by_table.get(name)
            if group:
                _plan_cyclic_group(db,c_contracts,group,actions)
                done.update(group['members'])
            else:_plan_table(db,c,actions)
        source_hash=digest_rows(db,'source_row');target_hash=digest_rows(db,'target_row')
        status='blocked' if db.execute('SELECT 1 FROM issue LIMIT 1').fetchone() or db.execute('SELECT 1 FROM collision_group LIMIT 1').fetchone() else 'ready'
        pid=db.execute('INSERT INTO migration_plan(source_hash,target_hash,status,actions,contract_hash,groups) VALUES (?,?,?,?,?,?)',
                       (source_hash,target_hash,status,json.dumps(actions,sort_keys=True),contract_digest(db),
                        json.dumps(groups,sort_keys=True))).lastrowid
    db.close();return pid,status,actions
def _steps(actions,groups):
    out=[];i=0
    while i<len(actions):
        a=actions[i];gid=a.get('group_id')
        if gid is None:out.append((a['table'],a['new'],a['kind']));i+=1;continue
        group=groups.get(gid);batch=[]
        while i+len(batch)<len(actions) and actions[i+len(batch)].get('group_id')==gid:batch.append(actions[i+len(batch)])
        if group['strategy']=='nullable_staging':
            out.extend((x['table'],x['new'],'stage_insert') for x in batch)
            out.extend((b['child_table'],b['child_new'],'backfill') for b in group['backfills'])
        else:out.extend((x['table'],x['new'],x['kind']) for x in batch)
        i+=len(batch)
    return out
def _verify_group(db,group,batch):
    by_key={(a['table'],a['new']):a for a in batch}
    for action in batch:
        row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(action['table'],action['new'])).fetchone()
        if not row or json.loads(row['payload'])!=action['payload']:raise ValueError('group_reference_unclosed')
    for edge in group['edges']:
        parent=db.execute('SELECT 1 FROM target_row WHERE table_name=? AND new_key=?',(edge['parent_table'],edge['parent_new'])).fetchone()
        action=by_key.get((edge['child_table'],edge['child_new']))
        if not parent or not action:raise ValueError('group_reference_unclosed')
        if action['payload'].get(edge['storage_field'])!=edge['parent_new']:raise ValueError('group_reference_unclosed')
def _batch(db,table,payloads):
    db.executemany('INSERT INTO target_row VALUES (?,?,?)',[(t,n,json.dumps(p,sort_keys=True)) for t,n,p in payloads])
def apply(path,plan_id,fail_after=None):
    db=connect(path)
    try:
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p or p['status']!='ready':raise ValueError('plan_not_ready')
            if p['source_hash']!=digest_rows(db,'source_row') or p['target_hash']!=digest_rows(db,'target_row') or p['contract_hash']!=contract_digest(db):raise ValueError('stale_plan')
            actions=json.loads(p['actions']);groups={g['id']:g for g in json.loads(p['groups'] or '[]')}
            step=0;i=0
            def record(table,new,kind):
                nonlocal step
                db.execute('INSERT INTO apply_step(plan_id,table_name,new_key,action) VALUES (?,?,?,?)',(plan_id,table,new,kind))
                step+=1
                if fail_after==step:raise RuntimeError('injected_failure')
            while i<len(actions):
                a=actions[i];gid=a.get('group_id')
                if gid is None:
                    if a['kind']=='insert':
                        db.execute('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',(plan_id,a['table'],a['new']))
                        db.execute('INSERT INTO target_row VALUES (?,?,?)',(a['table'],a['new'],json.dumps(a['payload'],sort_keys=True)))
                    record(a['table'],a['new'],a['kind']);i+=1;continue
                group=groups[gid];batch=[]
                while i+len(batch)<len(actions) and actions[i+len(batch)].get('group_id')==gid:batch.append(actions[i+len(batch)])
                inserts=[x for x in batch if x['kind']=='insert']
                if group['strategy']=='deferred':
                    _batch(db,'target_row',[(x['table'],x['new'],x['payload']) for x in inserts])
                    db.executemany('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',
                                   [(plan_id,x['table'],x['new']) for x in inserts])
                    for x in batch:record(x['table'],x['new'],x['kind'])
                else:
                    _batch(db,'target_row',[(x['table'],x['new'],x['staged_payload']) for x in inserts])
                    db.executemany('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',
                                   [(plan_id,x['table'],x['new']) for x in inserts])
                    for x in batch:record(x['table'],x['new'],'stage_insert')
                    staged={(x['table'],x['new']):x['staged_payload'] for x in inserts}
                    for backfill in group['backfills']:
                        key=(backfill['child_table'],backfill['child_new'])
                        if key in staged:
                            payload=staged[key];payload[backfill['storage_field']]=backfill['parent_new']
                            db.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                                       (json.dumps(payload,sort_keys=True),)+key)
                        record(backfill['child_table'],backfill['child_new'],'backfill')
                _verify_group(db,group,batch);i+=len(batch)
            db.execute("UPDATE migration_plan SET status='applied' WHERE id=?",(plan_id,))
    finally:db.close()
def rollback(path,plan_id):
    db=connect(path)
    with db:
        p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
        if not p or p['status']!='applied':raise ValueError('plan_not_applied')
        actions=json.loads(p['actions']);groups={g['id']:g for g in json.loads(p['groups'] or '[]')}
        steps=db.execute('SELECT table_name,new_key,action FROM apply_step WHERE plan_id=? ORDER BY id',(plan_id,)).fetchall()
        expected=_steps(actions,groups)
        if len(steps)!=len(expected) or any(tuple(step)!=want for step,want in zip(steps,expected)):
            raise ValueError('apply_ledger_changed')
        entries=db.execute('SELECT table_name,new_key FROM rollback_entry WHERE plan_id=?',(plan_id,)).fetchall()
        owned={(r['table_name'],r['new_key']) for r in entries}
        expected_owned={(a['table'],a['new']) for a in actions if a['kind']=='insert'}
        if owned!=expected_owned or len(entries)!=len(expected_owned):raise ValueError('rollback_ledger_changed')
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
                    field=_storage_field(ref)
                    if (ref['table'],str(payload.get(field))) in owned:
                        raise ValueError('rollback_referenced_by_external_row')
        for r in db.execute('SELECT * FROM rollback_entry WHERE plan_id=? ORDER BY id DESC',(plan_id,)).fetchall():db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(r['table_name'],r['new_key']))
        db.execute('DELETE FROM apply_step WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(plan_id,));db.execute("UPDATE migration_plan SET status='rolled_back' WHERE id=?",(plan_id,))
    db.close()
