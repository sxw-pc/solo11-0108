import csv,json
import pytest
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows,import_target
from keybridge.rules import plan,apply,rollback,resume,digest_rows
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
def crashed_checkpointed(db,pid,**kw):
    with pytest.raises(RuntimeError,match='injected_failure'):apply(db,pid,mode='checkpointed',**kw)
def status_of(db,pid):
    return connect(db).execute('SELECT status FROM migration_plan WHERE id=?',(pid,)).fetchone()[0]
def test_checkpointed_batch_failure_rolls_back_only_failed_batch(tmp_path):
    db=setup(tmp_path);pid,status,actions=plan(db);assert status=='ready'
    crashed_checkpointed(db,pid,fail_after=2)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==1
    assert c.execute('SELECT COUNT(*) FROM target_row WHERE table_name=?',('parent',)).fetchone()[0]==1
    assert status_of(db,pid)=='applying'
    assert tuple(c.execute('SELECT next_batch,total_batches FROM plan_cursor WHERE plan_id=?',(pid,)).fetchone())==(1,2)
    assert c.execute('SELECT COUNT(*) FROM apply_batch WHERE plan_id=?',(pid,)).fetchone()[0]==1
    assert resume(db,pid)=='applied'
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
    assert status_of(db,pid)=='applied'
def test_checkpointed_failure_inside_first_batch_commits_nothing(tmp_path):
    db=setup(tmp_path,parents=[['p1','A'],['p2','B']],children=[['c1','C','p1']])
    pid,_,_=plan(db);before=digest_rows(connect(db),'target_row')
    crashed_checkpointed(db,pid,fail_after=1)
    c=connect(db)
    assert digest_rows(c,'target_row')==before
    assert status_of(db,pid)=='ready'
    assert c.execute('SELECT COUNT(*) FROM plan_cursor').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM apply_batch').fetchone()[0]==0
    with pytest.raises(ValueError,match='not_checkpointed'):resume(db,pid)
    apply(db,pid,mode='checkpointed')
    assert connect(db).execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==3
def test_checkpointed_crash_between_batches_report_and_resume(tmp_path):
    from keybridge.report import write
    db=setup(tmp_path);pid,_,actions=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    c=connect(db)
    assert status_of(db,pid)=='applying'
    parent=c.execute('SELECT payload FROM target_row WHERE table_name=?',('parent',)).fetchone()
    assert parent and json.loads(parent['payload'])==actions[0]['payload']
    assert c.execute('SELECT COUNT(*) FROM target_row WHERE table_name=?',('child',)).fetchone()[0]==0
    data=json.loads(write(db,pid,tmp_path/'report').read_text(encoding='utf-8'))
    assert data['progress']['complete'] is False
    assert data['progress']['mode']=='checkpointed'
    assert data['progress']['available_tables']==['parent']
    assert data['progress']['pending_tables']==['child']
    assert [b['status'] for b in data['progress']['batches']]==['committed','pending']
    assert resume(db,pid)=='applied'
    data=json.loads(write(db,pid,tmp_path/'report2').read_text(encoding='utf-8'))
    assert data['progress']['complete'] is True and data['progress']['pending_tables']==[]
def test_checkpointed_duplicate_resume_is_idempotent(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    assert resume(db,pid)=='applied'
    snap=digest_rows(connect(db),'target_row')
    steps=connect(db).execute('SELECT COUNT(*) FROM apply_step WHERE plan_id=?',(pid,)).fetchone()[0]
    assert resume(db,pid)=='applied'
    c=connect(db)
    assert digest_rows(c,'target_row')==snap
    assert c.execute('SELECT COUNT(*) FROM apply_step WHERE plan_id=?',(pid,)).fetchone()[0]==steps
    assert c.execute('SELECT COUNT(*) FROM apply_batch WHERE plan_id=?',(pid,)).fetchone()[0]==2
    assert c.execute('SELECT COUNT(*) FROM rollback_entry WHERE plan_id=?',(pid,)).fetchone()[0]==2
def test_resume_blocks_source_change(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    with connect(db) as c:c.execute('UPDATE source_row SET natural=? WHERE old_key=?',('changed','p1'))
    with pytest.raises(ValueError,match='stale_plan'):resume(db,pid)
    assert status_of(db,pid)=='applying'
def test_resume_blocks_contract_change(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    with connect(db) as c:c.execute("UPDATE table_contract SET pk=? WHERE name=?",('id|extra','parent'))
    with pytest.raises(ValueError,match='contract_changed'):resume(db,pid)
    assert status_of(db,pid)=='applying'
def test_resume_blocks_completed_row_change_without_overwrite(tmp_path):
    db=setup(tmp_path);pid,_,actions=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    with connect(db) as c:c.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                     ('{"external":"edited"}','parent',actions[0]['new']))
    with pytest.raises(ValueError,match='resume_completed_changed'):resume(db,pid)
    row=connect(db).execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',('parent',actions[0]['new'])).fetchone()
    assert json.loads(row['payload'])=={'external':'edited'}
    assert status_of(db,pid)=='applying'
def test_resume_blocks_pending_scope_change_without_overwrite(tmp_path):
    db=setup(tmp_path);pid,_,actions=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    child_new=actions[1]['new']
    with connect(db) as c:c.execute('INSERT INTO target_row VALUES (?,?,?)',('child',child_new,json.dumps({'code':'external'})))
    with pytest.raises(ValueError,match='resume_scope_changed'):resume(db,pid)
    row=connect(db).execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',('child',child_new)).fetchone()
    assert json.loads(row['payload'])=={'code':'external'}
    assert status_of(db,pid)=='applying'
def test_resume_blocks_ledger_tampering(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    with connect(db) as c:c.execute('DELETE FROM rollback_entry WHERE plan_id=?',(pid,))
    with pytest.raises(ValueError,match='rollback_ledger_changed'):resume(db,pid)
    assert status_of(db,pid)=='applying'
def test_checkpointed_prefix_rollback_restores_target_and_reopens_plan(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db);before=digest_rows(connect(db),'target_row')
    crashed_checkpointed(db,pid,fail_after_batch=1)
    assert status_of(db,pid)=='applying'
    rollback(db,pid)
    c=connect(db)
    assert digest_rows(c,'target_row')==before
    assert status_of(db,pid)=='ready'
    for table in ('plan_cursor','apply_batch','apply_step','rollback_entry'):
        assert c.execute(f'SELECT COUNT(*) FROM {table} WHERE plan_id=?',(pid,)).fetchone()[0]==0
    apply(db,pid)
    assert connect(db).execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
def test_checkpointed_prefix_rollback_respects_external_references(tmp_path):
    db=setup(tmp_path);pid,_,actions=plan(db)
    crashed_checkpointed(db,pid,fail_after_batch=1)
    parent=actions[0]['new']
    with connect(db) as c:c.execute('INSERT INTO target_row VALUES (?,?,?)',
                     ('child','EXT-1',json.dumps({'code':'external','parent_id':parent})))
    with pytest.raises(ValueError,match='rollback_referenced_by_external_row'):rollback(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==2
    assert status_of(db,pid)=='applying'
    with connect(db) as c:c.execute("DELETE FROM target_row WHERE new_key='EXT-1'")
    rollback(db,pid)
    assert connect(db).execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==0
    assert status_of(db,pid)=='ready'
def test_atomic_and_checkpointed_are_distinct_run_contracts(tmp_path):
    da=tmp_path/'atomic';da.mkdir();db_a=setup(da)
    dc=tmp_path/'checkpointed';dc.mkdir();db_c=setup(dc)
    pid_a,_,_=plan(db_a);pid_c,_,_=plan(db_c)
    before_a=digest_rows(connect(db_a),'target_row');before_c=digest_rows(connect(db_c),'target_row')
    with pytest.raises(RuntimeError,match='injected_failure'):apply(db_a,pid_a,fail_after=1)
    assert digest_rows(connect(db_a),'target_row')==before_a
    assert status_of(db_a,pid_a)=='ready'
    crashed_checkpointed(db_c,pid_c,fail_after_batch=1)
    c=connect(db_c)
    assert digest_rows(c,'target_row')!=before_c
    assert c.execute('SELECT COUNT(*) FROM target_row WHERE table_name=?',('parent',)).fetchone()[0]==1
    assert status_of(db_c,pid_c)=='applying'
    with pytest.raises(ValueError,match='plan_not_ready'):apply(db_c,pid_c)
