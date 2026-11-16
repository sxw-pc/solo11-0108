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
        source_hash=digest_rows(db,'source_row');target_hash=digest_rows(db,'target_row');contract_hash=digest_rows(db,'table_contract')
        status='blocked' if db.execute('SELECT 1 FROM issue LIMIT 1').fetchone() or db.execute('SELECT 1 FROM collision_group LIMIT 1').fetchone() else 'ready'
        pid=db.execute('INSERT INTO migration_plan(source_hash,target_hash,status,actions,contract_hash) VALUES (?,?,?,?,?)',(source_hash,target_hash,status,json.dumps(actions,sort_keys=True),contract_hash)).lastrowid
    db.close();return pid,status,actions
def _batches(actions):
    """按拓扑安全的表边界把计划动作分批（plan 已按契约拓扑序生成动作）。"""
    batches=[];index={}
    for a in actions:
        t=a['table']
        if t not in index:index[t]=len(batches);batches.append([])
        batches[index[t]].append(a)
    return batches
def _batch_digest(batch):return hashlib.sha256(json.dumps(batch,sort_keys=True).encode()).hexdigest()
def _summarize(batch):
    return [{'table':a['table'],'old':a['old'],'new':a['new'],'kind':a['kind'],
             'sha':hashlib.sha256(json.dumps(a['payload'],sort_keys=True).encode()).hexdigest()} for a in batch]
def _verify_owned_rows(db,inserts,error='rollback_conflict'):
    for a in inserts:
        current=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(a['table'],a['new'])).fetchone()
        if not current or json.loads(current['payload'])!=a['payload']:raise ValueError(error)
def _check_external_refs(db,owned):
    for contract in db.execute('SELECT name,refs FROM table_contract'):
        for row in db.execute('SELECT new_key,payload FROM target_row WHERE table_name=?',(contract['name'],)):
            if (contract['name'],row['new_key']) in owned:continue
            payload=json.loads(row['payload'])
            for ref in json.loads(contract['refs']):
                fields=ref['field'] if isinstance(ref['field'],list) else [ref['field']]
                field=fields[0] if len(fields)==1 else '__ref_'+ref['table']+'_new_key'
                if (ref['table'],str(payload.get(field))) in owned:
                    raise ValueError('rollback_referenced_by_external_row')
def _verify_committed(db,plan_id,batches,done,row_error='resume_completed_changed'):
    """续跑/回滚前核对已提交批：批序号与动作摘要、apply_step、rollback_entry、目标行都必须仍吻合。"""
    for seq in range(done):
        batch=batches[seq]
        summary=db.execute('SELECT * FROM apply_batch WHERE plan_id=? AND seq=?',(plan_id,seq)).fetchone()
        if not summary or summary['table_name']!=batch[0]['table'] or summary['action_digest']!=_batch_digest(batch):
            raise ValueError('batch_mismatch')
        steps=db.execute('SELECT table_name,new_key,action FROM apply_step WHERE plan_id=? AND batch_seq=? ORDER BY id',(plan_id,seq)).fetchall()
        if [tuple(s) for s in steps]!=[(a['table'],a['new'],a['kind']) for a in batch]:
            raise ValueError('apply_ledger_changed')
        entries=db.execute('SELECT table_name,new_key FROM rollback_entry WHERE plan_id=? AND batch_seq=?',(plan_id,seq)).fetchall()
        expected={(a['table'],a['new']) for a in batch if a['kind']=='insert'}
        if {(e['table_name'],e['new_key']) for e in entries}!=expected or len(entries)!=len(expected):
            raise ValueError('rollback_ledger_changed')
        _verify_owned_rows(db,[a for a in batch if a['kind']=='insert'],row_error)
    if db.execute('SELECT 1 FROM apply_batch WHERE plan_id=? AND seq>=? LIMIT 1',(plan_id,done)).fetchone():
        raise ValueError('batch_mismatch')
def _verify_pending_scope(db,pending):
    """未执行范围不得被外部修改：待插入键必须仍不存在，待复用行必须仍是计划时的值。"""
    for batch in pending:
        for a in batch:
            row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(a['table'],a['new'])).fetchone()
            if a['kind']=='insert':
                if row:raise ValueError('resume_scope_changed')
            elif not row or json.loads(row['payload'])!=a['payload']:
                raise ValueError('resume_scope_changed')
def apply(path,plan_id,fail_after=None,mode='atomic',fail_after_batch=None):
    """mode='atomic'（默认）单事务全量执行；mode='checkpointed' 显式选择按批提交、可续跑。"""
    if mode=='checkpointed':return _checkpoint(path,plan_id,fail_after,fail_after_batch,True)
    if mode!='atomic':raise ValueError('unknown_mode')
    if fail_after_batch is not None:raise ValueError('fail_after_batch_requires_checkpointed')
    db=connect(path)
    try:
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p or p['status']!='ready':raise ValueError('plan_not_ready')
            if p['source_hash']!=digest_rows(db,'source_row') or p['target_hash']!=digest_rows(db,'target_row'):raise ValueError('stale_plan')
            if p['contract_hash']!=digest_rows(db,'table_contract'):raise ValueError('contract_changed')
            for n,a in enumerate(json.loads(p['actions']),1):
                if a['kind']=='insert':
                    db.execute('INSERT INTO rollback_entry(plan_id,table_name,new_key) VALUES (?,?,?)',(plan_id,a['table'],a['new']))
                    db.execute('INSERT INTO target_row VALUES (?,?,?)',(a['table'],a['new'],json.dumps(a['payload'],sort_keys=True)))
                db.execute('INSERT INTO apply_step(plan_id,table_name,new_key,action) VALUES (?,?,?,?)',(plan_id,a['table'],a['new'],a['kind']))
                if fail_after==n:raise RuntimeError('injected_failure')
            db.execute("UPDATE migration_plan SET status='applied' WHERE id=?",(plan_id,))
    finally:db.close()
def resume(path,plan_id,fail_after=None,fail_after_batch=None):
    """对同一 plan_id 续跑 checkpointed 执行；已提交批按批序号与动作摘要幂等跳过。"""
    return _checkpoint(path,plan_id,fail_after,fail_after_batch,False)
def _checkpoint(path,plan_id,fail_after,fail_after_batch,start):
    db=connect(path)
    try:
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p:raise ValueError('plan_not_ready')
            cursor=db.execute('SELECT * FROM plan_cursor WHERE plan_id=?',(plan_id,)).fetchone()
            if start:
                if p['status']!='ready':raise ValueError('plan_not_ready')
            else:
                if not cursor:raise ValueError('not_checkpointed')
                if p['status'] not in ('applying','applied'):raise ValueError('plan_not_applying')
            if p['source_hash']!=digest_rows(db,'source_row'):raise ValueError('stale_plan')
            if p['contract_hash']!=digest_rows(db,'table_contract'):raise ValueError('contract_changed')
            if start and p['target_hash']!=digest_rows(db,'target_row'):raise ValueError('stale_plan')
            actions=json.loads(p['actions']);batches=_batches(actions);done=0
            if cursor:
                if cursor['total_batches']!=len(batches):raise ValueError('batch_mismatch')
                done=cursor['next_batch']
                _verify_committed(db,plan_id,batches,done)
            _verify_pending_scope(db,batches[done:])
        if not batches:
            with db:db.execute("UPDATE migration_plan SET status='applied' WHERE id=?",(plan_id,))
            return 'applied'
        written=sum(len(b) for b in batches[:done])
        for seq in range(done,len(batches)):
            batch=batches[seq]
            with db:
                # 同一事务：目标写入 + 逆操作账本 + apply_step + 游标 + 动作摘要
                for a in batch:
                    written+=1
                    if a['kind']=='insert':
                        db.execute('INSERT INTO rollback_entry(plan_id,table_name,new_key,batch_seq) VALUES (?,?,?,?)',(plan_id,a['table'],a['new'],seq))
                        db.execute('INSERT INTO target_row VALUES (?,?,?)',(a['table'],a['new'],json.dumps(a['payload'],sort_keys=True)))
                    db.execute('INSERT INTO apply_step(plan_id,table_name,new_key,action,batch_seq) VALUES (?,?,?,?,?)',(plan_id,a['table'],a['new'],a['kind'],seq))
                    if fail_after==written:raise RuntimeError('injected_failure')
                db.execute('INSERT INTO apply_batch(plan_id,seq,table_name,actions,action_digest) VALUES (?,?,?,?,?)',
                           (plan_id,seq,batch[0]['table'],json.dumps(_summarize(batch),sort_keys=True),_batch_digest(batch)))
                db.execute('INSERT OR REPLACE INTO plan_cursor(plan_id,mode,next_batch,total_batches) VALUES (?,?,?,?)',
                           (plan_id,'checkpointed',seq+1,len(batches)))
                db.execute('UPDATE migration_plan SET status=? WHERE id=?',('applied' if seq==len(batches)-1 else 'applying',plan_id))
            if fail_after_batch==seq+1:raise RuntimeError('injected_failure')
        return 'applied'
    finally:db.close()
def rollback(path,plan_id):
    db=connect(path)
    try:
        with db:
            p=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not p or p['status'] not in ('applied','applying'):raise ValueError('plan_not_applied')
            actions=json.loads(p['actions'])
            if p['status']=='applying':_rollback_prefix(db,plan_id,actions)
            else:_rollback_applied(db,plan_id,actions)
    finally:db.close()
def _delete_owned(db,plan_id):
    for r in db.execute('SELECT * FROM rollback_entry WHERE plan_id=? ORDER BY id DESC',(plan_id,)).fetchall():
        db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(r['table_name'],r['new_key']))
    db.execute('DELETE FROM apply_step WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(plan_id,))
    db.execute('DELETE FROM apply_batch WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM plan_cursor WHERE plan_id=?',(plan_id,))
def _rollback_applied(db,plan_id,actions):
    steps=db.execute('SELECT table_name,new_key,action FROM apply_step WHERE plan_id=? ORDER BY id',(plan_id,)).fetchall()
    if len(steps)!=len(actions) or any(tuple(step)!=(a['table'],a['new'],a['kind']) for step,a in zip(steps,actions)):
        raise ValueError('apply_ledger_changed')
    entries=db.execute('SELECT table_name,new_key FROM rollback_entry WHERE plan_id=?',(plan_id,)).fetchall()
    owned={(r['table_name'],r['new_key']) for r in entries}
    expected={(a['table'],a['new']) for a in actions if a['kind']=='insert'}
    if owned!=expected or len(entries)!=len(expected):raise ValueError('rollback_ledger_changed')
    _verify_owned_rows(db,[a for a in actions if a['kind']=='insert'])
    _check_external_refs(db,owned)
    _delete_owned(db,plan_id)
    db.execute("UPDATE migration_plan SET status='rolled_back' WHERE id=?",(plan_id,))
def _rollback_prefix(db,plan_id,actions):
    """applying 计划：按逆依赖顺序回滚已提交前缀，验证计划归属与外部引用保护后回到 ready。"""
    cursor=db.execute('SELECT * FROM plan_cursor WHERE plan_id=?',(plan_id,)).fetchone()
    if not cursor:raise ValueError('not_checkpointed')
    batches=_batches(actions);done=cursor['next_batch']
    if done<1 or done>len(batches):raise ValueError('batch_mismatch')
    _verify_committed(db,plan_id,batches,done,row_error='rollback_conflict')
    committed=[a for b in batches[:done] for a in b]
    steps=db.execute('SELECT table_name,new_key,action FROM apply_step WHERE plan_id=? ORDER BY id',(plan_id,)).fetchall()
    if len(steps)!=len(committed) or any(tuple(s)!=(a['table'],a['new'],a['kind']) for s,a in zip(steps,committed)):
        raise ValueError('apply_ledger_changed')
    entries=db.execute('SELECT table_name,new_key FROM rollback_entry WHERE plan_id=?',(plan_id,)).fetchall()
    owned={(r['table_name'],r['new_key']) for r in entries}
    expected={(a['table'],a['new']) for a in committed if a['kind']=='insert'}
    if owned!=expected or len(entries)!=len(expected):raise ValueError('rollback_ledger_changed')
    _verify_owned_rows(db,[a for a in committed if a['kind']=='insert'])
    _check_external_refs(db,owned)
    _delete_owned(db,plan_id)
    db.execute("UPDATE migration_plan SET status='ready' WHERE id=?",(plan_id,))
