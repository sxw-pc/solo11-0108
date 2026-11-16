import csv,json
import pytest
from keybridge.contract import compile_contract
from keybridge.identity import adjudicate
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
def collision_fixture(tmp):
    return setup(tmp,parents=[['p1','A'],['p2',' a ']],children=[['c1','C','p1'],['c2','D','p2']])
def test_merge_adjudication(tmp_path):
    from keybridge.report import write
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    pid,status,actions=plan(db)
    assert status=='ready'
    parents=[a for a in actions if a['table']=='parent']
    children=[a for a in actions if a['table']=='child']
    assert len(parents)==1 and len(children)==2
    merged=parents[0]['new']
    assert all(c['payload']['parent_id']==merged for c in children)
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM identity_map WHERE table_name='parent'").fetchone()[0]==2
    assert c.execute("SELECT COUNT(DISTINCT new_key) FROM identity_map WHERE table_name='parent'").fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM collision_group WHERE reason='natural_collision' AND resolution='merge'").fetchone()[0]==2
    edges=c.execute('SELECT child_old,parent_old,parent_new FROM reference_edge ORDER BY child_old').fetchall()
    assert [tuple(e) for e in edges]==[('c1','p1',merged),('c2','p2',merged)]
    data=json.loads(write(db,pid,tmp_path/'report').read_text(encoding='utf-8'))
    counts=data['counts']['parent']
    assert counts['input']==2 and counts['insert']==1 and counts['merged']==1 and counts['blocked']==0 and counts['conserved']
    assert data['counts']['child']['input']==2 and data['counts']['child']['insert']==2 and data['counts']['child']['conserved']
    apply(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==3
    assert c.execute("SELECT COUNT(*) FROM target_row WHERE table_name='parent'").fetchone()[0]==1
def test_keep_adjudication(tmp_path):
    from keybridge.report import write
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','keep','evidence: distinct legacy accounts',{'p1':{'id':'p1'},'p2':{'id':'p2'}})
    pid,status,actions=plan(db)
    assert status=='ready'
    parents={a['old']:a['new'] for a in actions if a['table']=='parent'}
    assert len(parents)==2 and parents['p1']!=parents['p2']
    children={a['old']:a for a in actions if a['table']=='child'}
    assert children['c1']['payload']['parent_id']==parents['p1']
    assert children['c2']['payload']['parent_id']==parents['p2']
    assert plan(db)[2]==actions
    c=connect(db)
    edges=c.execute('SELECT child_old,parent_new FROM reference_edge ORDER BY child_old').fetchall()
    assert [tuple(e) for e in edges]==[('c1',parents['p1']),('c2',parents['p2'])]
    assert c.execute("SELECT COUNT(*) FROM collision_group WHERE reason='natural_collision' AND resolution='keep'").fetchone()[0]==2
    data=json.loads(write(db,pid,tmp_path/'report').read_text(encoding='utf-8'))
    counts=data['counts']['parent']
    assert counts['input']==2 and counts['insert']==2 and counts['merged']==0 and counts['blocked']==0 and counts['conserved']
def test_collision_without_adjudication_blocked(tmp_path):
    db=collision_fixture(tmp_path)
    _,status,actions=plan(db)
    assert status=='blocked'
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM collision_group WHERE reason='natural_collision' AND resolution IS NULL").fetchone()[0]==2
    assert c.execute("SELECT COUNT(*) FROM issue WHERE code='natural_collision'").fetchone()[0]==1
    assert not any(a['table']=='parent' and a['old']=='p2' for a in actions)
    assert not any(a['old']=='c2' for a in actions)
def test_merge_blocked_by_target_natural_conflict(tmp_path):
    db=collision_fixture(tmp_path)
    import_target(db,'parent',csvfile(tmp_path,'t.csv',['new_key','code'],[['EXT-1','A']]))
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    _,status,actions=plan(db)
    assert status=='blocked'
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM issue WHERE code='natural_target_conflict'").fetchone()[0]==1
    assert not any(a['table']=='parent' for a in actions)
    row=c.execute("SELECT payload FROM target_row WHERE table_name='parent' AND new_key='EXT-1'").fetchone()[0]
    assert row==json.dumps({'new_key':'EXT-1','code':'A'},sort_keys=True)
def test_merge_payload_conflict_blocked(tmp_path):
    db=tmp_path/'x.db';contract=tmp_path/'contract.json'
    contract.write_text(json.dumps({'tables':[{'name':'parent','pk':'id','natural':'code'},{'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}),encoding='utf-8')
    compile_contract(contract,db)
    import_rows(db,'parent',csvfile(tmp_path,'p.csv',['id','code','site'],[['p1','A','x'],['p2',' a ','y']]))
    import_rows(db,'child',csvfile(tmp_path,'c.csv',['id','code','parent_id'],[['c1','C','p1'],['c2','D','p2']]))
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    _,status,_=plan(db)
    assert status=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='merge_payload_conflict'").fetchone()[0]==1
def test_keep_missing_discriminator_blocks_reference_closure(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','keep','evidence: distinct legacy accounts',{'p1':{'id':'p1'}})
    _,status,actions=plan(db)
    assert status=='blocked'
    c=connect(db)
    assert c.execute("SELECT COUNT(*) FROM issue WHERE code='missing_discriminator' AND subject='parent:p2'").fetchone()[0]==1
    assert c.execute("SELECT COUNT(*) FROM issue WHERE code='orphan_reference' AND subject='child:c2'").fetchone()[0]==1
    assert not any(a['old'] in ('p2','c2') for a in actions)
def test_adjudication_stale_on_source_change(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    c=connect(db)
    with c:c.execute("UPDATE source_row SET payload=? WHERE table_name='parent' AND old_key='p2'",
                     (json.dumps({'id':'p2','code':' a ','note':'changed'},sort_keys=True),))
    _,status,_=plan(db)
    assert status=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='adjudication_stale'").fetchone()[0]==1
def test_adjudication_stale_on_target_change(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    import_target(db,'parent',csvfile(tmp_path,'t.csv',['new_key','code'],[['EXT-1','Z']]))
    _,status,_=plan(db)
    assert status=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='adjudication_stale'").fetchone()[0]==1
def test_adjudication_stale_on_contract_change(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    c=connect(db)
    with c:c.execute("UPDATE table_contract SET refs='[]' WHERE name='child'")
    _,status,_=plan(db)
    assert status=='blocked'
    assert connect(db).execute("SELECT COUNT(*) FROM issue WHERE code='adjudication_stale'").fetchone()[0]==1
def test_apply_rechecks_decision_digest(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    pid,status,_=plan(db)
    assert status=='ready'
    c=connect(db)
    with c:c.execute("UPDATE adjudication SET evidence='tampered'")
    with pytest.raises(ValueError,match='stale_decision'):apply(db,pid)
def test_merge_apply_rollback_external_reference_protection(tmp_path):
    db=collision_fixture(tmp_path)
    adjudicate(db,'parent','a','merge','evidence: same upstream customer record')
    pid,status,actions=plan(db)
    assert status=='ready'
    before=digest_rows(connect(db),'target_row')
    apply(db,pid)
    merged=[a for a in actions if a['table']=='parent'][0]['new']
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==3
    with c:c.execute('INSERT INTO target_row VALUES (?,?,?)',
                     ('child','EXT-9',json.dumps({'code':'external','parent_id':merged})))
    with pytest.raises(ValueError,match='rollback_referenced_by_external_row'):rollback(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==4
    with c:c.execute("DELETE FROM target_row WHERE new_key='EXT-9'")
    rollback(db,pid)
    assert digest_rows(connect(db),'target_row')==before
def test_adjudication_validation(tmp_path):
    db=collision_fixture(tmp_path)
    with pytest.raises(ValueError,match='invalid_decision'):adjudicate(db,'parent','a','split','evidence')
    with pytest.raises(ValueError,match='missing_evidence'):adjudicate(db,'parent','a','merge','')
    with pytest.raises(ValueError,match='unknown_collision_group'):adjudicate(db,'parent','zz','merge','evidence')
    with pytest.raises(ValueError,match='unknown_discriminator_row'):adjudicate(db,'parent','a','keep','evidence',{'p9':{'id':'p9'}})
    with pytest.raises(ValueError,match='discriminator_mismatch'):adjudicate(db,'parent','a','keep','evidence',{'p1':{'id':'nope'}})
