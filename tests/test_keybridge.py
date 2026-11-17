import csv,json
import pytest
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows,import_target
from keybridge.rules import plan,apply,rollback,digest_rows
from keybridge.store import connect
def csvfile(tmp,name,header,rows):
    p=tmp/name
    with p.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(header);w.writerows(rows)
    return p
def setup(tmp,parents=None,children=None):
    db=tmp/'x.db';contract=tmp/'contract.json'
    contract.write_text(json.dumps({'tables':[{'name':'parent','pk':'id','natural':'code'},{'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}),encoding='utf-8')
    assert compile_contract(contract,db)==['parent','child']
    import_rows(db,'parent',csvfile(tmp,'p.csv',['id','code'],parents or [['p1','A']]))
    import_rows(db,'child',csvfile(tmp,'c.csv',['id','code','parent_id'],children or [['c1','C','p1']]))
    return db
def test_contract_cycle_and_missing(tmp_path):
    p=tmp_path/'contract.json';p.write_text(json.dumps({'tables':[{'name':'a','pk':'id','natural':'id','refs':[{'field':'x','table':'a'}]}]}))
    with pytest.raises(ValueError,match='reference_cycle'):compile_contract(p,tmp_path/'x.db')
    p.write_text(json.dumps({'tables':[{'name':'a','pk':'id','natural':'id','refs':[{'field':'x','table':'missing'}]}]}))
    with pytest.raises(ValueError,match='unknown_reference'):compile_contract(p,tmp_path/'x.db')
def test_import_idempotence_and_old_key_conflict(tmp_path):
    db=setup(tmp_path)
    assert import_rows(db,'parent',tmp_path/'p.csv')==0
    import_rows(db,'parent',csvfile(tmp_path,'p2.csv',['id','code'],[['p1','B']]))
    assert connect(db).execute('SELECT COUNT(*) FROM collision_group').fetchone()[0]==1
    assert plan(db)[1]=='blocked'
def test_deterministic_mapping_and_reference(tmp_path):
    db=setup(tmp_path);pid,status,actions=plan(db)
    assert status=='ready' and [a['table'] for a in actions]==['parent','child']
    assert actions[1]['payload']['parent_id']==actions[0]['new']
    assert plan(db)[2]==actions
    assert connect(db).execute('SELECT COUNT(*) FROM reference_edge').fetchone()[0]==1
def test_natural_collision(tmp_path):
    db=setup(tmp_path,parents=[['p1','A'],['p2',' a ']])
    assert plan(db)[1]=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='natural_collision'").fetchone()[0]==1
def test_orphan_reference(tmp_path):
    db=setup(tmp_path,children=[['c1','C','missing']]);_,status,actions=plan(db)
    assert status=='blocked' and len(actions)==1
def test_import_requires_declared_reference_fields(tmp_path):
    db=setup(tmp_path)
    bad=csvfile(tmp_path,'bad_child.csv',['id','code'],[['c2','D']])
    with pytest.raises(ValueError,match='missing_column'):import_rows(db,'child',bad)
    assert connect(db).execute("SELECT COUNT(*) FROM source_row WHERE old_key='c2'").fetchone()[0]==0
def test_apply_rollback_and_failure(tmp_path):
    db=setup(tmp_path);pid,status,actions=plan(db);assert status=='ready'
    before=digest_rows(connect(db),'target_row')
    with pytest.raises(RuntimeError,match='injected_failure'):apply(db,pid,fail_after=2)
    assert digest_rows(connect(db),'target_row')==before
    apply(db,pid);assert connect(db).execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
    rollback(db,pid);assert digest_rows(connect(db),'target_row')==before
def test_rollback_refuses_changed_owned_row_without_partial_delete(tmp_path):
    db=setup(tmp_path);pid,_,actions=plan(db);apply(db,pid)
    c=connect(db)
    with c:c.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                     ('{"external":"edited"}',actions[1]['table'],actions[1]['new']))
    with pytest.raises(ValueError,match='rollback_conflict'):rollback(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
    assert c.execute('SELECT status FROM migration_plan WHERE id=?',(pid,)).fetchone()[0]=='applied'
def test_rollback_requires_complete_apply_ledger(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db);apply(db,pid)
    c=connect(db)
    with c:c.execute('DELETE FROM rollback_entry WHERE id=(SELECT MAX(id) FROM rollback_entry)')
    with pytest.raises(ValueError,match='rollback_ledger_changed'):rollback(db,pid)
    assert connect(db).execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
def test_rollback_blocks_external_child_of_owned_parent(tmp_path):
    db=setup(tmp_path);pid,_,actions=plan(db);apply(db,pid)
    parent=actions[0]['new'];c=connect(db)
    with c:c.execute('INSERT INTO target_row VALUES (?,?,?)',
                     ('child','EXT-1',json.dumps({'code':'external','parent_id':parent})))
    with pytest.raises(ValueError,match='rollback_referenced_by_external_row'):rollback(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==3
    assert c.execute('SELECT status FROM migration_plan WHERE id=?',(pid,)).fetchone()[0]=='applied'
def test_stale_plan(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    with connect(db) as conn:conn.execute('UPDATE source_row SET natural=? WHERE old_key=?',('different','p1'))
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)
def test_target_conflict(tmp_path):
    db=setup(tmp_path);_,_,actions=plan(db)
    import_target(db,'parent',csvfile(tmp_path,'target.csv',['new_key','code'],[[actions[0]['new'],'wrong']]))
    assert plan(db)[1]=='blocked'
def test_target_natural_conflict(tmp_path):
    db=setup(tmp_path)
    import_target(db,'parent',csvfile(tmp_path,'target.csv',['new_key','code'],[['external','A']]))
    assert plan(db)[1]=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='natural_target_conflict'").fetchone()[0]==1
def test_target_snapshot_conflict_preserves_original_row(tmp_path):
    db=setup(tmp_path)
    first=csvfile(tmp_path,'target1.csv',['new_key','code'],[['TARGET','A']])
    changed=csvfile(tmp_path,'target2.csv',['new_key','code'],[['TARGET','B']])
    import_target(db,'parent',first)
    before=digest_rows(connect(db),'target_row')
    with pytest.raises(ValueError,match='target_snapshot_conflict'):import_target(db,'parent',changed)
    assert digest_rows(connect(db),'target_row')==before
def test_composite_primary_and_reference(tmp_path):
    db=tmp_path/'x.db';p=tmp_path/'contract.json'
    p.write_text(json.dumps({'tables':[{'name':'parent','pk':['site','local'],'natural':['site','code']},
      {'name':'child','pk':'id','natural':'code','refs':[{'field':['site','parent_local'],'table':'parent'}]}]}))
    compile_contract(p,db)
    import_rows(db,'parent',csvfile(tmp_path,'parent.csv',['site','local','code'],[['S','P','A']]))
    import_rows(db,'child',csvfile(tmp_path,'child.csv',['id','code','site','parent_local'],[['C','B','S','P']]))
    _,status,actions=plan(db)
    assert status=='ready' and actions[0]['payload']['__new_key']
    assert actions[1]['payload']['__ref_parent_new_key']==actions[0]['new']
def test_plan_report_counts_and_stale_target(tmp_path):
    from keybridge.report import write
    db=setup(tmp_path);pid,status,actions=plan(db)
    data=json.loads(write(db,pid,tmp_path/'report').read_text(encoding='utf-8'))
    assert data['counts']['parent']['input']==1 and data['edges'][0]['parent_new']==actions[0]['new']
    import_target(db,'parent',csvfile(tmp_path,'target.csv',['new_key','code'],[['external','other']]))
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)

# --- irreversible erasure ---------------------------------------------------
import io
from keybridge.rules import erase
from keybridge.corrections import import_corrections,prepare_corrections,apply_corrections
from keybridge.quality import register_contract as quality_contract,import_batch as quality_import,evaluate as quality_evaluate,propose_repair as quality_propose,review_repair as quality_review
def erasure_db(tmp):
    contract=tmp/'contract.json'
    contract.write_text(json.dumps({'tables':[{'name':'parent','pk':'id','natural':'code','sensitive':['email']},
      {'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}),encoding='utf-8')
    db=tmp/'erase.db';compile_contract(contract,db)
    import_rows(db,'parent',csvfile(tmp,'parents.csv',['id','code','email','region'],[['p1','A','p1@example.test','north'],['p2','B','p2@example.test','south']]))
    import_rows(db,'child',csvfile(tmp,'children.csv',['id','code','parent_id','phone'],[['c1','CA','p1','111'],['c2','CB','p1','222'],['c3','CC','p2','333']]))
    return db
def applied_erasure_db(tmp):
    db=erasure_db(tmp);pid,status,_=plan(db);assert status=='ready';apply(db,pid);return db,pid
def table_dump(db):
    con=connect(db);dump={}
    for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        dump[name]=[tuple(r) for r in con.execute('SELECT * FROM '+name+' ORDER BY 1,2')]
    return dump
def assert_value_gone(db,value):
    for table,rows in table_dump(db).items():
        for row in rows:assert value not in str(row),table
def csv_rows(raw):return list(csv.DictReader(io.StringIO(bytes(raw).decode('utf-8-sig'))))
def test_sensitive_field_cannot_be_key_or_reference(tmp_path):
    base={'tables':[{'name':'parent','pk':'id','natural':'code'},{'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}
    for i,(table,field) in enumerate([('parent','id'),('parent','code'),('child','parent_id')]):
        spec=json.loads(json.dumps(base))
        for t in spec['tables']:
            if t['name']==table:t['sensitive']=[field]
        p=tmp_path/('c%d.json'%i);p.write_text(json.dumps(spec),encoding='utf-8')
        with pytest.raises(ValueError,match='sensitive_key_field'):compile_contract(p,tmp_path/('x%d.db'%i))
def test_erasure_validation(tmp_path):
    db,_=applied_erasure_db(tmp_path)
    with pytest.raises(ValueError,match='unknown_scope'):erase(db,'parent','p1','peek',['email'],'r1')
    with pytest.raises(ValueError,match='invalid_request'):erase(db,'parent','p1','field_only',['email'],'')
    with pytest.raises(ValueError,match='unknown_table'):erase(db,'missing','p1','field_only',['email'],'r2')
    with pytest.raises(ValueError,match='field_not_sensitive'):erase(db,'parent','p1','field_only',['region'],'r3')
    with pytest.raises(ValueError,match='no_fields'):erase(db,'parent','p1','field_only',[],'r4')
    with pytest.raises(ValueError,match='unknown_entity'):erase(db,'parent','nope','field_only',['email'],'r5')
def test_field_only_erasure_keeps_rows_and_references(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    con=connect(db)
    child_sha=con.execute("SELECT sha FROM source_file WHERE id=(SELECT source_id FROM source_row WHERE table_name='child' AND old_key='c1')").fetchone()[0]
    receipt=erase(db,'parent','p1','field_only',['email'],'req-field-1')
    con=connect(db)
    assert receipt['entities']==[['parent','p1']] and receipt['affected_rows']==sum(receipt['affected'].values())
    assert (receipt['affected']['source_file'],receipt['affected']['source_row'],receipt['affected']['target_row'])==(1,1,1)
    assert receipt['affected']['migration_plan']==1 and receipt['affected']['rollback_entry']==5
    assert 'p1@example.test' not in json.dumps(receipt)
    stored=con.execute("SELECT * FROM erasure_request WHERE request_id='req-field-1'").fetchone()
    assert stored['status']=='completed' and stored['affected_rows']==receipt['affected_rows'] and 'p1@example.test' not in stored['receipt']
    assert stored['original_artifact_sha']!=stored['erased_artifact_sha']
    # entity row, mapping and references survive; only the email field is gone
    payload=json.loads(con.execute("SELECT payload FROM source_row WHERE table_name='parent' AND old_key='p1'").fetchone()[0])
    assert 'email' not in payload and payload['region']=='north' and payload['code']=='A'
    new_p1=con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    target=json.loads(con.execute("SELECT payload FROM target_row WHERE table_name='parent' AND new_key=?",(new_p1,)).fetchone()[0])
    assert 'email' not in target and target['region']=='north' and target['id']==new_p1
    assert con.execute("SELECT COUNT(*) FROM identity_map").fetchone()[0]==5
    assert con.execute("SELECT COUNT(*) FROM reference_edge").fetchone()[0]==3
    c1=json.loads(con.execute("SELECT payload FROM target_row WHERE table_name='child' AND new_key=(SELECT new_key FROM identity_map WHERE table_name='child' AND old_key='c1')").fetchone()[0])
    assert c1['parent_id']==new_p1
    # shared raw CSV keeps the other rows; the unrelated child file is untouched
    parent_raw=con.execute("SELECT raw FROM source_file WHERE id=(SELECT source_id FROM source_row WHERE table_name='parent' AND old_key='p2')").fetchone()[0]
    rows={r['id']:r for r in csv_rows(parent_raw)}
    assert rows['p1']['email']=='' and rows['p1']['region']=='north' and rows['p2']['email']=='p2@example.test'
    assert con.execute("SELECT sha FROM source_file WHERE id=(SELECT source_id FROM source_row WHERE table_name='child' AND old_key='c1')").fetchone()[0]==child_sha
    assert json.loads(con.execute("SELECT payload FROM source_row WHERE table_name='parent' AND old_key='p2'").fetchone()[0])['email']=='p2@example.test'
    # applied plan is sealed and can no longer be rolled back
    assert con.execute("SELECT status FROM migration_plan WHERE id=?",(pid,)).fetchone()[0]=='sealed'
    assert con.execute("SELECT COUNT(*) FROM rollback_entry WHERE plan_id=?",(pid,)).fetchone()[0]==0
    with pytest.raises(ValueError,match='plan_not_applied'):rollback(db,pid)
    assert_value_gone(db,'p1@example.test')
def test_entity_closure_erases_entity_and_descendants(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    receipt=erase(db,'parent','p1','entity_closure',['email'],'req-close-1')
    con=connect(db)
    assert receipt['entities']==[['child','c1'],['child','c2'],['parent','p1']]
    assert con.execute("SELECT COUNT(*) FROM identity_map WHERE old_key IN ('p1','c1','c2')").fetchone()[0]==0
    assert con.execute("SELECT COUNT(*) FROM source_row WHERE old_key IN ('p1','c1','c2')").fetchone()[0]==0
    assert {r[0] for r in con.execute("SELECT old_key FROM identity_map")}=={'p2','c3'}
    assert con.execute("SELECT COUNT(*) FROM target_row").fetchone()[0]==2
    edges=[(r['child_old'],r['parent_old']) for r in con.execute("SELECT child_old,parent_old FROM reference_edge")]
    assert edges==[('c3','p2')]
    row=con.execute("SELECT status,actions FROM migration_plan WHERE id=?",(pid,)).fetchone()
    assert row['status']=='sealed' and {a['old'] for a in json.loads(row['actions'])}=={'p2','c3'}
    with pytest.raises(ValueError,match='plan_not_applied'):rollback(db,pid)
    parent_raw=con.execute("SELECT raw FROM source_file WHERE id=(SELECT source_id FROM source_row WHERE table_name='parent' AND old_key='p2')").fetchone()[0]
    assert [r['id'] for r in csv_rows(parent_raw)]==['p2']
    child_raw=con.execute("SELECT raw FROM source_file WHERE id=(SELECT source_id FROM source_row WHERE table_name='child' AND old_key='c3')").fetchone()[0]
    assert [r['id'] for r in csv_rows(child_raw)]==['c3']
    p2=json.loads(con.execute("SELECT payload FROM source_row WHERE table_name='parent' AND old_key='p2'").fetchone()[0])
    assert p2['email']=='p2@example.test'
    guards={(r['table_name'],r['old_key'],r['field']) for r in con.execute('SELECT table_name,old_key,field FROM erasure_guard')}
    assert ('parent','p1','*') in guards and ('child','c1','*') in guards and ('child','c2','*') in guards
    assert_value_gone(db,'p1@example.test')
def test_entity_closure_rejects_external_target_reference(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    con=connect(db)
    new_p1=con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    with con:con.execute("INSERT INTO target_row VALUES ('child','EXT-1',?)",(json.dumps({'id':'ext','code':'X','parent_id':new_p1}),))
    before=table_dump(db)
    with pytest.raises(ValueError,match='erasure_closure_referenced'):
        erase(db,'parent','p1','entity_closure',['email'],'req-close-2')
    assert table_dump(db)==before
    assert connect(db).execute("SELECT COUNT(*) FROM erasure_request").fetchone()[0]==0
    # field_only keeps the row and every reference, so it still succeeds
    erase(db,'parent','p1','field_only',['email'],'req-field-2')
    con=connect(db)
    ext=json.loads(con.execute("SELECT payload FROM target_row WHERE new_key='EXT-1'").fetchone()[0])
    assert ext['parent_id']==new_p1
    assert con.execute("SELECT COUNT(*) FROM target_row WHERE new_key=?",(new_p1,)).fetchone()[0]==1
    assert_value_gone(db,'p1@example.test')
def test_ready_plan_goes_stale_on_erasure(tmp_path):
    db=erasure_db(tmp_path);pid,status,_=plan(db);assert status=='ready'
    erase(db,'parent','p1','field_only',['email'],'req-plan-1')
    assert connect(db).execute("SELECT status FROM migration_plan WHERE id=?",(pid,)).fetchone()[0]=='stale'
    with pytest.raises(ValueError,match='plan_not_ready'):apply(db,pid)
def test_erasure_request_replay_and_conflict(tmp_path):
    db,_=applied_erasure_db(tmp_path)
    first=erase(db,'parent','p1','field_only',['email'],'req-same')
    assert erase(db,'parent','p1','field_only',['email'],'req-same')==first
    assert connect(db).execute("SELECT COUNT(*) FROM erasure_request").fetchone()[0]==1
    with pytest.raises(ValueError,match='erasure_request_conflict'):erase(db,'parent','p1','entity_closure',['email'],'req-same')
def test_reimport_of_erased_entity_blocked_and_plan_rebuild_clean(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    new_p1=connect(db).execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    erase(db,'parent','p1','entity_closure',['email'],'req-block-1')
    files_before=connect(db).execute("SELECT COUNT(*) FROM source_file").fetchone()[0]
    with pytest.raises(ValueError,match='erased_entity_reimport'):import_rows(db,'parent',tmp_path/'parents.csv')
    with pytest.raises(ValueError,match='erased_entity_reimport'):import_rows(db,'child',tmp_path/'children.csv')
    target_csv=csvfile(tmp_path,'target.csv',['new_key','code'],[[new_p1,'A']])
    with pytest.raises(ValueError,match='erased_entity_reimport'):import_target(db,'parent',target_csv)
    con=connect(db)
    assert con.execute("SELECT COUNT(*) FROM source_file").fetchone()[0]==files_before
    assert con.execute("SELECT COUNT(*) FROM source_row WHERE old_key IN ('p1','c1','c2')").fetchone()[0]==0
    # rebuilding a plan cannot resurrect the erased entities or values
    _,status,actions=plan(db)
    assert status=='ready' and {a['old'] for a in actions}=={'p2','c3'}
    assert 'p1@example.test' not in json.dumps(actions)
def test_reimport_of_erased_field_blocked(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    new_p1=connect(db).execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    erase(db,'parent','p1','field_only',['email'],'req-block-2')
    with pytest.raises(ValueError,match='erased_field_reimport'):import_rows(db,'parent',tmp_path/'parents.csv')
    target_csv=csvfile(tmp_path,'target.csv',['new_key','code','email','region'],[[new_p1,'A','p1@example.test','north']])
    with pytest.raises(ValueError,match='erased_field_reimport'):import_target(db,'parent',target_csv)
    con=connect(db)
    assert con.execute("SELECT COUNT(*) FROM source_file").fetchone()[0]==2
    assert 'email' not in json.loads(con.execute("SELECT payload FROM source_row WHERE table_name='parent' AND old_key='p1'").fetchone()[0])
    # a different entity carrying the same field name is unaffected
    assert import_rows(db,'parent',csvfile(tmp_path,'p3.csv',['id','code','email','region'],[['p3','C','p3@example.test','west']]))==1
def test_erasure_fault_rolls_back_all_copies(tmp_path):
    db,pid=applied_erasure_db(tmp_path)
    first=tmp_path/'first.json'
    first.write_text(json.dumps([{'table':'parent','old_key':'p1','expected_revision':0,'after':{'id':'p1','code':'A','email':'p1@example.test','region':'north','label':'L'}}]),encoding='utf-8')
    batch=import_corrections(db,pid,first);prepare_corrections(db,batch);assert apply_corrections(db,batch)=='applied'
    # diverge the target on the sensitive field so a second correction conflicts on it
    con=connect(db)
    new_p1=con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    changed=json.loads(con.execute("SELECT payload FROM target_row WHERE table_name='parent' AND new_key=?",(new_p1,)).fetchone()[0])
    changed['email']='other@x.test'
    with con:con.execute("UPDATE target_row SET payload=? WHERE table_name='parent' AND new_key=?",(json.dumps(changed,sort_keys=True),new_p1))
    second=tmp_path/'second.json'
    second.write_text(json.dumps([{'table':'parent','old_key':'p1','expected_revision':1,'after':{'id':'p1','code':'A','email':'new@y.test','region':'north','label':'L2'}}]),encoding='utf-8')
    batch2=import_corrections(db,pid,second)
    assert prepare_corrections(db,batch2)[0][1:] == ('conflict','field_conflict')
    assert connect(db).execute("SELECT COUNT(*) FROM correction_conflict WHERE field='email'").fetchone()[0]==1
    before=table_dump(db)
    # fault after the source BLOB rewrite, before correction_head/correction_conflict cleanup
    with pytest.raises(RuntimeError,match='injected_failure'):
        erase(db,'parent','p1','field_only',['email'],'req-fault-1',fail_stage='after_source_blob')
    assert table_dump(db)==before
def test_erasure_scrubs_quality_repair_ledger(tmp_path):
    db=erasure_db(tmp_path)
    qc=tmp_path/'qc.json';qc.write_text(json.dumps({'id':'q-parent','table':'parent','identity_field':'id','required_fields':['id','code'],'numeric_ranges':{}}),encoding='utf-8')
    quality_contract(db,qc)
    batch_file=tmp_path/'qb.json';batch_file.write_text(json.dumps([
      {'source_id':'ext:1','payload':{'id':'p1','email':'p1@example.test'}},
      {'source_id':'ext:2','payload':{'id':'p2','code':'B','email':'p2@example.test'}}]),encoding='utf-8')
    batch=quality_import(db,'q-parent',batch_file)['batch_id']
    assert quality_evaluate(db,batch)['quarantined']==1
    sha=connect(db).execute("SELECT current_sha FROM quality_item WHERE batch_id=? AND row_no=1",(batch,)).fetchone()[0]
    prop=tmp_path/'prop.json';prop.write_text(json.dumps({'proposal_id':'fix-1','batch_id':batch,'row_no':1,'expected_sha':sha,'submitter':'ann','patch':{'code':'A','email':'p1@example.test'}}),encoding='utf-8')
    quality_propose(db,prop);quality_review(db,'fix-1','bob',True,'ok')
    assert 'p1@example.test' in connect(db).execute("SELECT patch_json FROM quality_repair WHERE id='fix-1'").fetchone()[0]
    erase(db,'parent','p1','field_only',['email'],'req-quality-1')
    con=connect(db)
    item=con.execute("SELECT original_json,current_json FROM quality_item WHERE batch_id=? AND row_no=1",(batch,)).fetchone()
    assert 'email' not in json.loads(item['original_json']) and 'email' not in json.loads(item['current_json'])
    patch=json.loads(con.execute("SELECT patch_json FROM quality_repair WHERE id='fix-1'").fetchone()[0])
    assert 'email' not in patch and patch['code']=='A'
    assert json.loads(con.execute("SELECT current_json FROM quality_item WHERE batch_id=? AND row_no=2",(batch,)).fetchone()[0])['email']=='p2@example.test'
    raw=json.loads(con.execute("SELECT raw FROM quality_batch WHERE id=?",(batch,)).fetchone()[0])
    assert 'email' not in raw[0]['payload'] and raw[1]['payload']['email']=='p2@example.test'
    assert_value_gone(db,'p1@example.test')
