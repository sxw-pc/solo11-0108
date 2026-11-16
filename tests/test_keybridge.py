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


def cycle_setup(tmp,deferrable=None,nullable=None,child_rows=None):
    """A<->B cycle plus external child C->A, with target capability declarations."""
    tmp.mkdir(parents=True,exist_ok=True)
    db=tmp/'cycle.db';p=tmp/'cycle-contract.json'
    def edge(field,table):
        ref={'field':field,'table':table}
        if deferrable is not None:ref['deferrable']=deferrable
        if nullable is not None:ref['nullable']=nullable
        return ref
    p.write_text(json.dumps({'tables':[
        {'name':'a','pk':'id','natural':'code','refs':[edge('b_id','b')]},
        {'name':'b','pk':'id','natural':'code','refs':[edge('a_id','a')]},
        {'name':'c','pk':'id','natural':'code','refs':[{'field':'a_id','table':'a'}]}]}),encoding='utf-8')
    compile_contract(p,db)
    import_rows(db,'a',csvfile(tmp,'a.csv',['id','code','b_id'],[['a1','A','b1']]))
    import_rows(db,'b',csvfile(tmp,'b.csv',['id','code','a_id'],[['b1','B','a1']]))
    import_rows(db,'c',csvfile(tmp,'c.csv',['id','code','a_id'],child_rows or [['c1','C','a1']]))
    return db
def new_key(db,table,old):
    return connect(db).execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',(table,old)).fetchone()['new_key']
def assert_references_closed(db):
    c=connect(db)
    rows={(r['table_name'],r['new_key']):json.loads(r['payload']) for r in c.execute('SELECT * FROM target_row')}
    edges=[dict(e) for e in c.execute('SELECT * FROM reference_edge')]
    assert edges
    for e in edges:
        child=new_key(db,e['child_table'],e['child_old'])
        assert rows[(e['child_table'],child)][e['field']]==e['parent_new']
        assert (e['parent_table'],e['parent_new']) in rows
    return edges,rows
def test_cycle_requires_declared_target_capability(tmp_path):
    p=tmp_path/'contract.json'
    p.write_text(json.dumps({'tables':[{'name':'a','pk':'id','natural':'id','refs':[{'field':'b_id','table':'b'}]},
      {'name':'b','pk':'id','natural':'id','refs':[{'field':'a_id','table':'a'}]}]}))
    with pytest.raises(ValueError,match='reference_cycle'):compile_contract(p,tmp_path/'x.db')
    p.write_text(json.dumps({'tables':[{'name':'a','pk':'id','natural':'id','refs':[{'field':'b_id','table':'b','deferrable':False}]},
      {'name':'b','pk':'id','natural':'id','refs':[{'field':'a_id','table':'a'}]}]}))
    with pytest.raises(ValueError,match='reference_cycle'):compile_contract(p,tmp_path/'y.db')
    p.write_text(json.dumps({'tables':[{'name':'a','pk':'id','natural':'id','refs':[{'field':'b_id','table':'b','deferrable':'yes'}]},
      {'name':'b','pk':'id','natural':'id','refs':[{'field':'a_id','table':'a','deferrable':True}]}]}))
    with pytest.raises(ValueError,match='invalid_capability'):compile_contract(p,tmp_path/'z.db')
def test_deferred_cycle_applies_as_one_atomic_group(tmp_path):
    db=cycle_setup(tmp_path,deferrable=True,nullable=False)
    pid,status,actions=plan(db);assert status=='ready'
    con=connect(db);groups=json.loads(con.execute('SELECT groups FROM migration_plan WHERE id=?',(pid,)).fetchone()['groups'])
    assert len(groups)==1 and groups[0]['members']==['a','b'] and groups[0]['strategy']=='deferred'
    assert {a['table'] for a in actions if a.get('group_id') is not None}=={'a','b'}
    assert next(a for a in actions if a['table']=='c').get('group_id') is None
    assert con.execute('SELECT COUNT(*) FROM issue').fetchone()[0]==0
    apply(db,pid)
    edges,rows=assert_references_closed(db)
    assert len(rows)==3 and len(edges)==3
    cyclic=[e for e in edges if e['cyclic']]
    assert len(cyclic)==2 and all(e['target_deferrable']==1 and not e['backfill'] for e in cyclic)
    steps=[tuple(r) for r in con.execute('SELECT table_name,action FROM apply_step WHERE plan_id=? ORDER BY id',(pid,))]
    assert steps==[('a','insert'),('b','insert'),('c','insert')]
    assert con.execute('SELECT COUNT(*) FROM apply_step WHERE action IN (?,?)',('stage_insert','backfill')).fetchone()[0]==0
    from keybridge.cutover import audit_target
    assert audit_target(db,pid)[1]=='complete'
    rollback(db,pid)
    assert con.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==0
def test_nullable_cycle_stages_entities_then_backfills_all_edges(tmp_path):
    db=cycle_setup(tmp_path,deferrable=False,nullable=True)
    pid,status,actions=plan(db);assert status=='ready'
    con=connect(db);groups=json.loads(con.execute('SELECT groups FROM migration_plan WHERE id=?',(pid,)).fetchone()['groups'])
    group=groups[0]
    assert group['members']==['a','b'] and group['strategy']=='nullable_staging'
    assert {b['child_table'] for b in group['backfills']}=={'a','b'} and len(group['backfills'])==2
    staged=[a for a in actions if a.get('group_id') is not None]
    assert staged[0]['staged_payload']['b_id'] is None and staged[1]['staged_payload']['a_id'] is None
    apply(db,pid)
    edges,rows=assert_references_closed(db)
    cyclic=[e for e in edges if e['cyclic']]
    assert len(cyclic)==2 and all(e['target_nullable']==1 and e['backfill']==1 for e in cyclic)
    steps=[tuple(r) for r in con.execute('SELECT table_name,action FROM apply_step WHERE plan_id=? ORDER BY id',(pid,))]
    assert steps==[('a','stage_insert'),('b','stage_insert'),('a','backfill'),('b','backfill'),('c','insert')]
    from keybridge.cutover import audit_target
    assert audit_target(db,pid)[1]=='complete'
    rollback(db,pid)
    assert con.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==0
def test_non_deferrable_non_nullable_cycle_is_blocked(tmp_path):
    db=cycle_setup(tmp_path,deferrable=False,nullable=False)
    pid,status,actions=plan(db)
    assert status=='blocked' and {a['table'] for a in actions}=={'c'}
    con=connect(db)
    assert con.execute("SELECT COUNT(*) FROM issue WHERE code='cycle_not_applyable'").fetchone()[0]==1
    groups=json.loads(con.execute('SELECT groups FROM migration_plan WHERE id=?',(pid,)).fetchone()['groups'])
    assert groups[0]['strategy']=='blocked' and groups[0]['members']==['a','b']
    with pytest.raises(ValueError,match='plan_not_ready'):apply(db,pid)
    assert con.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==0
def test_cycle_group_preserves_external_child_outside_scc(tmp_path):
    db=cycle_setup(tmp_path,deferrable=True,nullable=False)
    _,_,actions=plan(db)
    con=connect(db)
    assert {a['table'] for a in actions if a.get('group_id') is not None}=={'a','b'}
    assert con.execute("SELECT group_id FROM reference_edge WHERE child_table='c'").fetchone()[0] is None
    assert con.execute("SELECT cyclic FROM reference_edge WHERE child_table='c'").fetchone()[0]==0
    from keybridge.report import write
    pid=plan(db)[0];data=json.loads(write(db,pid,tmp_path/'r').read_text(encoding='utf-8'))
    assert data['groups'][0]['members']==['a','b']
    assert [e['parent_new'] for e in data['edges'] if e['child_table']=='c']==[new_key(db,'a','a1')]
def test_cycle_plan_stale_on_contract_source_and_target_changes(tmp_path):
    def fresh(name):
        root=tmp_path/name;root.mkdir()
        return cycle_setup(root,deferrable=True,nullable=False)
    # contract capability change
    db=fresh('contract');pid=plan(db)[0]
    with connect(db) as con:
        con.execute("UPDATE table_contract SET refs=? WHERE name='a'",
                    (json.dumps([{'field':'b_id','table':'b','deferrable':False,'nullable':True}]),))
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)
    # source change
    db=fresh('source');pid=plan(db)[0]
    with connect(db) as con:con.execute("UPDATE source_row SET natural='different' WHERE table_name='a' AND old_key='a1'")
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)
    # target snapshot change
    db=fresh('target');pid,_,_=plan(db)
    import_target(db,'c',csvfile(tmp_path,'ext.csv',['new_key','code','a_id'],[['EXT','X',new_key(db,'a','a1')]]))
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)
def test_two_phase_interruption_leaves_no_partial_references(tmp_path):
    db=cycle_setup(tmp_path,deferrable=False,nullable=True)
    pid,status,_=plan(db);assert status=='ready'
    before=digest_rows(connect(db),'target_row')
    for fail_after in (1,2,3,4,5):
        with pytest.raises(RuntimeError,match='injected_failure'):apply(db,pid,fail_after=fail_after)
        con=connect(db)
        assert digest_rows(con,'target_row')==before
        assert con.execute('SELECT COUNT(*) FROM target_row').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM apply_step').fetchone()[0]==0
        assert con.execute('SELECT COUNT(*) FROM rollback_entry').fetchone()[0]==0
        assert con.execute('SELECT status FROM migration_plan WHERE id=?',(pid,)).fetchone()[0]=='ready'
    apply(db,pid)
    assert_references_closed(db)
    assert connect(db).execute("SELECT COUNT(*) FROM apply_step WHERE action='backfill'").fetchone()[0]==2
def test_cycle_reference_conservation_and_blocked_report(tmp_path):
    from keybridge.report import write
    db=cycle_setup(tmp_path,deferrable=False,nullable=True)
    pid,_,actions=plan(db)
    data=json.loads(write(db,pid,tmp_path/'report').read_text(encoding='utf-8'))
    assert data['groups'][0]['members']==['a','b'] and data['groups'][0]['strategy']=='nullable_staging'
    assert {(b['child_table'],b['field']) for b in data['groups'][0]['backfills']}=={('a','b_id'),('b','a_id')}
    assert {(e['child_table'],e['field'],e['parent_new']) for e in data['edges']}=={
        ('a','b_id',new_key(db,'b','b1')),('b','a_id',new_key(db,'a','a1')),('c','a_id',new_key(db,'a','a1'))}
    apply(db,pid);edges,rows=assert_references_closed(db)
    assert sum(1 for e in edges if e['cyclic'])==2 and len(rows)==3
    db2=cycle_setup(tmp_path/'blocked',deferrable=False,nullable=False)
    pid2,status2,_=plan(db2)
    blocked=json.loads(write(db2,pid2,tmp_path/'blocked-report').read_text(encoding='utf-8'))
    assert status2=='blocked'
    assert [i['code'] for i in blocked['issues']]==['cycle_not_applyable']
