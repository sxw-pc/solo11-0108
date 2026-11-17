import csv,hashlib,io,json
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
        for r in db.execute('SELECT * FROM rollback_entry WHERE plan_id=? ORDER BY id DESC',(plan_id,)).fetchall():db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(r['table_name'],r['new_key']))
        db.execute('DELETE FROM apply_step WHERE plan_id=?',(plan_id,));db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(plan_id,));db.execute("UPDATE migration_plan SET status='rolled_back' WHERE id=?",(plan_id,))
    db.close()

# --- irreversible erasure -------------------------------------------------
#
# erase() removes every application-database copy of the requested values in
# one SQLite transaction: shared raw CSV/JSON blobs, source_row/target_row,
# migration_plan.actions, the correction and quality-repair ledgers and any
# target_audit_detail containing the values.  Applied plans that touched the
# entity are sealed (rollback ledger destroyed), pending plans and correction
# batches go stale, and erasure_guard rows block any later import or
# correction that would re-introduce an erased field.  The receipt records
# the original artifact hashes as irreversible credential plus the
# post-erasure hashes and affected row counts; it never contains the values.
def _canon(v):return json.dumps(v,sort_keys=True,ensure_ascii=False,separators=(',',':'))
def _erasure_closure(db,table,old_key):
    contracts={r['name']:json.loads(r['refs']) for r in db.execute('SELECT name,refs FROM table_contract')}
    rows={}
    for r in db.execute('SELECT table_name,old_key,payload FROM source_row'):
        rows.setdefault(r['table_name'],[]).append((r['old_key'],json.loads(r['payload'])))
    closure={(table,old_key)};grew=True
    while grew:
        grew=False
        for child,refs in contracts.items():
            for ref in refs:
                fields=ref['field'] if isinstance(ref['field'],list) else [ref['field']]
                keys={k for t,k in closure if t==ref['table']}
                if not keys:continue
                for key,payload in rows.get(child,[]):
                    if (child,key) not in closure and '|'.join(str(payload.get(f,'')) for f in fields) in keys:
                        closure.add((child,key));grew=True
    return closure
def _external_target_reference(db,mapped):
    contracts={r['name']:json.loads(r['refs']) for r in db.execute('SELECT name,refs FROM table_contract')}
    for row in db.execute('SELECT table_name,new_key,payload FROM target_row'):
        if (row['table_name'],row['new_key']) in mapped:continue
        payload=json.loads(row['payload'])
        for ref in contracts.get(row['table_name'],[]):
            fields=ref['field'] if isinstance(ref['field'],list) else [ref['field']]
            value=payload.get(fields[0]) if len(fields)==1 else payload.get('__ref_'+ref['table']+'_new_key')
            if value is not None and (ref['table'],str(value)) in mapped:return row['table_name']+':'+row['new_key']
    return None
def erase(path,table,old_key,scope,fields,request_id,fail_stage=None):
    if scope not in ('field_only','entity_closure'):raise ValueError('unknown_scope')
    if not request_id:raise ValueError('invalid_request')
    fields=fields or []
    if any(not isinstance(f,str) or not f for f in fields):raise ValueError('invalid_request')
    fields=sorted(set(fields))
    db=connect(path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM table_contract WHERE name=?',(table,)).fetchone():raise ValueError('unknown_table')
            sensitive={r['field'] for r in db.execute('SELECT field FROM table_sensitive WHERE table_name=?',(table,))}
            if scope=='field_only' and not fields:raise ValueError('no_fields')
            if any(f not in sensitive for f in fields):raise ValueError('field_not_sensitive')
            previous=db.execute('SELECT * FROM erasure_request WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if (previous['table_name'],previous['old_key'],previous['scope'],json.loads(previous['fields']))!=(table,old_key,scope,fields):
                    raise ValueError('erasure_request_conflict')
                return json.loads(previous['receipt'])
            if not db.execute('SELECT 1 FROM source_row WHERE table_name=? AND old_key=?',(table,old_key)).fetchone():raise ValueError('unknown_entity')
            entities=_erasure_closure(db,table,old_key) if scope=='entity_closure' else {(table,old_key)}
            mapped={}
            for t,k in entities:
                m=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(t,k)).fetchone()
                if m:mapped[(t,k)]=m['new_key']
            if scope=='entity_closure' and _external_target_reference(db,{(t,nk) for (t,k),nk in mapped.items()}):
                raise ValueError('erasure_closure_referenced')
            contracts={r['name']:r for r in db.execute('SELECT * FROM table_contract')}
            affected={};original_sha={};erased_sha={}
            def bump(key,n=1):affected[key]=affected.get(key,0)+n
            # 1. shared raw CSV blobs: drop closure rows, blank erased field cells
            per_file={}
            for t,k in entities:
                s=db.execute('SELECT source_id FROM source_row WHERE table_name=? AND old_key=?',(t,k)).fetchone()
                if s and s['source_id'] is not None:per_file.setdefault(s['source_id'],[]).append((t,k))
            new_keys=set(mapped.values())
            for f in db.execute('SELECT id,sha,raw FROM source_file').fetchall():
                rows=list(csv.reader(io.StringIO(bytes(f['raw']).decode('utf-8-sig'))))
                if not rows:continue
                header=rows[0];linked=[]
                for t,k in per_file.get(f['id'],[]):
                    cols=contracts[t]['pk'].split('|')
                    if all(c in header for c in cols):linked.append(([header.index(c) for c in cols],k))
                nk_idx=header.index('new_key') if 'new_key' in header else None
                blank_idx=[header.index(x) for x in fields if x in header]
                out=[header];changed=False
                for r in rows[1:]:
                    hit=any(len(r)>max(idx) and '|'.join(r[i] for i in idx)==k for idx,k in linked)
                    hit=hit or (nk_idx is not None and len(r)>nk_idx and r[nk_idx] in new_keys)
                    if hit:
                        if scope=='entity_closure':changed=True;continue
                        for i in blank_idx:
                            if i<len(r) and r[i]!='':r[i]='';changed=True
                    out.append(r)
                if not changed:continue
                buf=io.StringIO();csv.writer(buf).writerows(out);new_raw=buf.getvalue().encode()
                original_sha['source_file:%d'%f['id']]=f['sha']
                erased_sha['source_file:%d'%f['id']]=hashlib.sha256(new_raw).hexdigest()
                db.execute('UPDATE source_file SET raw=?,sha=? WHERE id=?',(new_raw,erased_sha['source_file:%d'%f['id']],f['id']))
                bump('source_file')
            if fail_stage=='after_source_blob':raise RuntimeError('injected_failure')
            # 2. source rows, identity mapping and reference edges
            for t,k in sorted(entities):
                if scope=='entity_closure':
                    bump('source_row',db.execute('DELETE FROM source_row WHERE table_name=? AND old_key=?',(t,k)).rowcount)
                    bump('identity_map',db.execute('DELETE FROM identity_map WHERE table_name=? AND old_key=?',(t,k)).rowcount)
                    bump('reference_edge',db.execute('DELETE FROM reference_edge WHERE (child_table=? AND child_old=?) OR (parent_table=? AND parent_old=?)',(t,k,t,k)).rowcount)
                else:
                    payload=json.loads(db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?',(t,k)).fetchone()['payload'])
                    if any(x in payload for x in fields):
                        for x in fields:payload.pop(x,None)
                        db.execute('UPDATE source_row SET payload=? WHERE table_name=? AND old_key=?',(json.dumps(payload,sort_keys=True),t,k))
                        bump('source_row')
            # 3. target rows
            for (t,k),nk in sorted(mapped.items()):
                if scope=='entity_closure':
                    bump('target_row',db.execute('DELETE FROM target_row WHERE table_name=? AND new_key=?',(t,nk)).rowcount)
                else:
                    row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',(t,nk)).fetchone()
                    if row:
                        payload=json.loads(row['payload'])
                        if any(x in payload for x in fields):
                            for x in fields:payload.pop(x,None)
                            db.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',(json.dumps(payload,sort_keys=True),t,nk))
                            bump('target_row')
            # 4. migration plans: scrub actions, seal applied plans, stale pending ones
            for p in db.execute('SELECT id,status,actions FROM migration_plan').fetchall():
                actions=json.loads(p['actions'])
                if not any((a['table'],a['old']) in entities for a in actions):continue
                if scope=='entity_closure':actions=[a for a in actions if (a['table'],a['old']) not in entities]
                else:
                    for a in actions:
                        if (a['table'],a['old'])==(table,old_key):
                            for x in fields:a['payload'].pop(x,None)
                db.execute('UPDATE migration_plan SET actions=? WHERE id=?',(json.dumps(actions,sort_keys=True),p['id']))
                if p['status']=='applied':
                    db.execute("UPDATE migration_plan SET status='sealed' WHERE id=?",(p['id'],))
                    bump('rollback_entry',db.execute('DELETE FROM rollback_entry WHERE plan_id=?',(p['id'],)).rowcount)
                elif p['status'] in ('ready','blocked'):db.execute("UPDATE migration_plan SET status='stale' WHERE id=?",(p['id'],))
                bump('migration_plan')
            # 5. correction batches, items, heads and conflict ledgers
            batches=set()
            for it in db.execute('SELECT id,batch_id,table_name,old_key,after_source,desired_target,merged_target FROM correction_item').fetchall():
                if (it['table_name'],it['old_key']) not in entities:continue
                batches.add(it['batch_id'])
                if scope=='entity_closure':
                    bump('correction_conflict',db.execute('DELETE FROM correction_conflict WHERE item_id=?',(it['id'],)).rowcount)
                    bump('correction_item',db.execute('DELETE FROM correction_item WHERE id=?',(it['id'],)).rowcount)
                    continue
                after=json.loads(it['after_source'])
                desired=json.loads(it['desired_target']) if it['desired_target'] else None
                merged=json.loads(it['merged_target']) if it['merged_target'] else None
                changed=False
                for obj in (after,desired,merged):
                    if obj:
                        for x in fields:
                            if x in obj:obj.pop(x);changed=True
                bump('correction_conflict',db.execute('DELETE FROM correction_conflict WHERE item_id=? AND field IN (%s)'%','.join('?'*len(fields)),[it['id']]+fields).rowcount)
                if changed:
                    db.execute('UPDATE correction_item SET after_source=?,desired_target=?,merged_target=? WHERE id=?',
                               (_canon(after),_canon(desired) if desired is not None else None,_canon(merged) if merged is not None else None,it['id']))
                    bump('correction_item')
            for t,k in sorted(entities):
                if scope=='entity_closure':
                    bump('correction_head',db.execute('DELETE FROM correction_head WHERE table_name=? AND old_key=?',(t,k)).rowcount)
                    continue
                head=db.execute('SELECT source_payload,projected_payload,target_payload FROM correction_head WHERE table_name=? AND old_key=?',(t,k)).fetchone()
                if not head:continue
                payloads={c:json.loads(head[c]) for c in ('source_payload','projected_payload','target_payload')}
                if any(x in p for p in payloads.values() for x in fields):
                    for p in payloads.values():
                        for x in fields:p.pop(x,None)
                    db.execute('UPDATE correction_head SET source_payload=?,projected_payload=?,target_payload=? WHERE table_name=? AND old_key=?',
                               (_canon(payloads['source_payload']),_canon(payloads['projected_payload']),_canon(payloads['target_payload']),t,k))
                    bump('correction_head')
            for bid in sorted(batches):
                b=db.execute('SELECT source_sha,raw,status FROM correction_batch WHERE id=?',(bid,)).fetchone()
                items=json.loads(bytes(b['raw']))
                if scope=='entity_closure':items=[r for r in items if (str(r.get('table')),str(r.get('old_key'))) not in entities]
                else:
                    for r in items:
                        if (str(r.get('table')),str(r.get('old_key')))==(table,old_key) and isinstance(r.get('after'),dict):
                            for x in fields:r['after'].pop(x,None)
                new_raw=json.dumps(items,ensure_ascii=False).encode()
                if new_raw!=bytes(b['raw']):
                    original_sha['correction_batch:%d'%bid]=b['source_sha']
                    erased_sha['correction_batch:%d'%bid]=hashlib.sha256(new_raw).hexdigest()
                    db.execute('UPDATE correction_batch SET raw=?,source_sha=? WHERE id=?',(new_raw,erased_sha['correction_batch:%d'%bid],bid))
                if b['status'] in ('received','prepared'):db.execute("UPDATE correction_batch SET status='stale' WHERE id=?",(bid,))
                bump('correction_batch')
            # 6. quality quarantine and repair ledgers
            for qc in db.execute('SELECT * FROM quality_contract').fetchall():
                keys={k for t,k in entities if t==qc['table_name']}
                if not keys:continue
                ident=qc['identity_field']
                for b in db.execute('SELECT id,source_sha,raw FROM quality_batch WHERE contract_id=?',(qc['id'],)).fetchall():
                    items=json.loads(bytes(b['raw']));changed=False;kept=[]
                    for item in items:
                        payload=item.get('payload') if isinstance(item,dict) else None
                        if isinstance(payload,dict) and str(payload.get(ident)) in keys:
                            if scope=='entity_closure':changed=True;continue
                            for x in fields:
                                if x in payload:payload.pop(x);changed=True
                        kept.append(item)
                    if changed:
                        new_raw=json.dumps(kept,ensure_ascii=False).encode()
                        original_sha['quality_batch:%d'%b['id']]=b['source_sha']
                        erased_sha['quality_batch:%d'%b['id']]=hashlib.sha256(new_raw).hexdigest()
                        db.execute('UPDATE quality_batch SET raw=?,source_sha=? WHERE id=?',(new_raw,erased_sha['quality_batch:%d'%b['id']],b['id']))
                        bump('quality_batch')
                for it in db.execute('SELECT qi.batch_id,qi.row_no,qi.original_json,qi.current_json FROM quality_item qi JOIN quality_batch qb ON qb.id=qi.batch_id WHERE qb.contract_id=?',(qc['id'],)).fetchall():
                    current=json.loads(it['current_json'])
                    if str(current.get(ident)) not in keys:continue
                    if scope=='entity_closure':
                        bump('quality_repair',db.execute('DELETE FROM quality_repair WHERE batch_id=? AND row_no=?',(it['batch_id'],it['row_no'])).rowcount)
                        bump('quality_item',db.execute('DELETE FROM quality_item WHERE batch_id=? AND row_no=?',(it['batch_id'],it['row_no'])).rowcount)
                        continue
                    original=json.loads(it['original_json']);changed=False
                    for x in fields:
                        if x in original:original.pop(x);changed=True
                        if x in current:current.pop(x);changed=True
                    if changed:
                        canon=_canon(current)
                        db.execute('UPDATE quality_item SET original_json=?,current_json=?,current_sha=? WHERE batch_id=? AND row_no=?',
                                   (_canon(original),canon,hashlib.sha256(canon.encode()).hexdigest(),it['batch_id'],it['row_no']))
                        bump('quality_item')
                    for rep in db.execute('SELECT id,patch_json FROM quality_repair WHERE batch_id=? AND row_no=?',(it['batch_id'],it['row_no'])).fetchall():
                        patch=json.loads(rep['patch_json'])
                        if any(x in patch for x in fields):
                            for x in fields:patch.pop(x,None)
                            db.execute('UPDATE quality_repair SET patch_json=? WHERE id=?',(_canon(patch),rep['id']))
                            bump('quality_repair')
            # 7. audit details containing the values
            for (t,k),nk in sorted(mapped.items()):
                if scope=='entity_closure':
                    bump('target_audit_detail',db.execute('DELETE FROM target_audit_detail WHERE table_name=? AND new_key=?',(t,nk)).rowcount)
                    continue
                for d in db.execute('SELECT audit_id,expected,actual FROM target_audit_detail WHERE table_name=? AND new_key=?',(t,nk)).fetchall():
                    values={};changed=False
                    for col in ('expected','actual'):
                        try:obj=json.loads(d[col])
                        except ValueError:obj=None
                        if isinstance(obj,dict):
                            for x in fields:
                                if x in obj:obj.pop(x);changed=True
                            values[col]=json.dumps(obj,sort_keys=True)
                        else:values[col]=d[col]
                    if changed:
                        db.execute('UPDATE target_audit_detail SET expected=?,actual=? WHERE audit_id=? AND table_name=? AND new_key=?',
                                   (values['expected'],values['actual'],d['audit_id'],t,nk))
                        bump('target_audit_detail')
            # 8. old retirement conclusions no longer match the target summary
            bump('retirement_gate',db.execute("UPDATE retirement_gate SET status='stale' WHERE status='retire_old_keys' AND audit_id IN (SELECT id FROM target_audit WHERE source_digest<>? OR target_digest<>?)",
                                              (digest_rows(db,'source_row'),digest_rows(db,'target_row'))).rowcount)
            # 9. re-introduction guards and the irreversible receipt
            for t,k in sorted(entities):
                nk=mapped.get((t,k))
                if scope=='entity_closure':db.execute('INSERT OR REPLACE INTO erasure_guard VALUES (?,?,?,?,?)',(t,k,nk,'*',request_id))
                else:
                    for x in fields:db.execute('INSERT OR REPLACE INTO erasure_guard VALUES (?,?,?,?,?)',(t,k,nk,x,request_id))
            receipt={'request_id':request_id,'table':table,'old_key':old_key,'scope':scope,'fields':fields,
                     'entities':sorted([list(e) for e in entities]),'original_artifact_sha':original_sha,
                     'erased_artifact_sha':erased_sha,'affected':affected,'affected_rows':sum(affected.values())}
            db.execute('INSERT INTO erasure_request(request_id,table_name,old_key,scope,fields,entities,original_artifact_sha,erased_artifact_sha,affected_rows,receipt,status) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id,table,old_key,scope,json.dumps(fields),json.dumps(receipt['entities']),json.dumps(original_sha,sort_keys=True),
                        json.dumps(erased_sha,sort_keys=True),receipt['affected_rows'],json.dumps(receipt,sort_keys=True),'completed'))
            return receipt
    finally:db.close()
