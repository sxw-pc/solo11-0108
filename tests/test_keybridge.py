import csv,json
import pytest
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows,import_target,import_target_generation
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


def generations(db):
    return [dict(r) for r in connect(db).execute('SELECT * FROM target_generation ORDER BY id')]

def generation_rows(db,gid):
    return {(r['table_name'],r['new_key']):json.loads(r['payload'])
            for r in connect(db).execute('SELECT * FROM target_generation_row WHERE generation_id=?',(gid,))}

def active_id(db):
    return connect(db).execute('SELECT generation_id FROM target_active WHERE slot=1').fetchone()[0]

def generation_setup(tmp):
    db=tmp/'g.db';contract=tmp/'g.json'
    contract.write_text(json.dumps({'tables':[
        {'name':'c','pk':'id','natural':'code'},
        {'name':'o','pk':'id','natural':'code','refs':[{'field':'c_id','table':'c'}]}]}),encoding='utf-8')
    compile_contract(contract,db)
    c=csvfile(tmp,'g0-c.csv',['new_key','code'],[['C1','one'],['C2','two']])
    o=csvfile(tmp,'g0-o.csv',['new_key','code','c_id'],[['O1','order','C1']])
    g0=import_target_generation(db,{'c':c,'o':o},'full_snapshot')
    return db,g0

def test_missing_rows_retired_by_full_snapshot_but_kept_by_delta(tmp_path):
    db,g0=generation_setup(tmp_path)
    only_c2=csvfile(tmp_path,'c2.csv',['new_key','code'],[['C2','two']])
    delta=import_target(db,'c',only_c2,mode='delta',base_generation=g0)
    rows=generation_rows(db,delta)
    assert set(rows)=={('c','C1'),('c','C2'),('o','O1')}
    assert active_id(db)==delta and generations(db)[-1]['mode']=='delta'
    full_root=tmp_path/'full';full_root.mkdir()
    fdb,fg0=generation_setup(full_root)
    full=import_target_generation(fdb,{'c':csvfile(full_root,'c2.csv',['new_key','code'],[['C2','two']]),
                                       'o':csvfile(full_root,'empty-o.csv',['new_key','code','c_id'],[])},'full_snapshot')
    assert set(generation_rows(fdb,full))=={('c','C2')}
    assert active_id(fdb)==full

def test_parent_removal_with_live_child_rejects_whole_generation(tmp_path):
    db,g0=generation_setup(tmp_path)
    before=generation_rows(db,g0);count=len(generations(db))
    only_c2=csvfile(tmp_path,'c2.csv',['new_key','code'],[['C2','two']])
    with pytest.raises(ValueError,match='reference_closure_violation'):
        import_target_generation(db,{'c':only_c2},'full_snapshot')
    assert active_id(db)==g0 and len(generations(db))==count
    assert generation_rows(db,g0)==before

def test_child_and_parent_removed_together_publish_atomically(tmp_path):
    db,g0=generation_setup(tmp_path)
    old=generation_rows(db,g0)
    only_c2=csvfile(tmp_path,'c2.csv',['new_key','code'],[['C2','two']])
    empty_o=csvfile(tmp_path,'empty-o.csv',['new_key','code','c_id'],[])
    g1=import_target_generation(db,{'c':only_c2,'o':empty_o},'full_snapshot')
    assert active_id(db)==g1 and set(generation_rows(db,g1))=={('c','C2')}
    assert generation_rows(db,g0)==old

def test_same_sha_is_a_distinct_contract_in_full_and_delta(tmp_path):
    db,g0=generation_setup(tmp_path)
    only_c2=csvfile(tmp_path,'c2.csv',['new_key','code'],[['C2','two']])
    empty_o=csvfile(tmp_path,'empty-o.csv',['new_key','code','c_id'],[])
    files={'c':only_c2,'o':empty_o}
    delta=import_target_generation(db,files,'delta',base_generation=g0)
    assert set(generation_rows(db,delta))=={('c','C1'),('c','C2'),('o','O1')}
    full=import_target_generation(db,files,'full_snapshot')
    assert set(generation_rows(db,full))=={('c','C2')}
    assert delta!=full
    imports=[tuple(r) for r in connect(db).execute('SELECT table_name,mode,source_sha,base_generation FROM target_generation_import ORDER BY id')]
    modes={(row[0],row[1]) for row in imports}
    assert modes=={('c','delta'),('o','delta'),('c','full_snapshot'),('o','full_snapshot')}
    count=len(generations(db))
    assert import_target_generation(db,files,'delta',base_generation=g0)==delta
    assert import_target_generation(db,files,'full_snapshot')==full
    assert len(generations(db))==count

def test_stale_delta_base_cannot_overwrite_newer_generation(tmp_path):
    db,g0=generation_setup(tmp_path)
    first=csvfile(tmp_path,'d1.csv',['new_key','code'],[['C2','winner']])
    winner=import_target(db,'c',first,mode='delta',base_generation=g0)
    stale=csvfile(tmp_path,'d2.csv',['new_key','code'],[['C2','loser']])
    with pytest.raises(ValueError,match='stale_base_generation'):
        import_target(db,'c',stale,mode='delta',base_generation=g0)
    assert active_id(db)==winner
    assert generation_rows(db,winner)[('c','C2')]['code']=='winner'
    assert len(generations(db))==2
    with pytest.raises(ValueError,match='base_generation_required'):
        import_target(db,'c',stale,mode='delta')

def test_plan_is_bound_to_target_generation_and_rejects_newer_import(tmp_path):
    db=setup(tmp_path)
    pid,status,_=plan(db);assert status=='ready'
    row=connect(db).execute('SELECT target_generation_id,target_generation_digest FROM migration_plan WHERE id=?',(pid,)).fetchone()
    gid=row['target_generation_id'];assert gid is not None and row['target_generation_digest']
    original=generation_rows(db,gid)
    ext=csvfile(tmp_path,'external.csv',['new_key','code'],[['EXT','external']])
    import_target(db,'parent',ext,mode='delta',base_generation=gid)
    with pytest.raises(ValueError,match='stale_plan'):apply(db,pid)
    assert generation_rows(db,gid)==original

def seeded_target(tmp,parents=None,children=None):
    db=setup(tmp,parents,children);pid,status,actions=plan(db);assert status=='ready';apply(db,pid)
    gid=connect(db).execute('SELECT applied_generation_id FROM migration_plan WHERE id=?',(pid,)).fetchone()[0]
    return db,pid,actions,gid

def _payload_sha(payload):
    import hashlib
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def test_old_audit_and_gate_keep_pointing_at_their_generation(tmp_path):
    from keybridge.cutover import audit_target,configure_clients,observe_lookup,retirement_decision
    db,pid,actions,g0=seeded_target(tmp_path)
    clients=tmp_path/'clients.json'
    clients.write_text(json.dumps([{'id':'catalog','critical':True,'table':'parent'}]),encoding='utf-8')
    configure_clients(db,clients)
    parent_new=actions[0]['new']
    observe_lookup(db,'catalog',pid,1,'p1',parent_new,_payload_sha(actions[0]['payload']))
    aid,status,_=audit_target(db,pid)
    assert status=='complete'
    gate,decision,_=retirement_decision(db,aid)
    assert decision=='retire_old_keys'
    audit_before=dict(connect(db).execute('SELECT * FROM target_audit WHERE id=?',(aid,)).fetchone())
    gate_before=dict(connect(db).execute('SELECT * FROM retirement_gate WHERE id=?',(gate,)).fetchone())
    member_before=[tuple(r) for r in connect(db).execute('SELECT * FROM retirement_member WHERE gate_id=? ORDER BY old_key',(gate,))]
    assert audit_before['target_generation_id']==g0
    g0_rows=generation_rows(db,g0)
    assert ('parent',parent_new) in g0_rows and len(g0_rows)==2

    newer=csvfile(tmp_path,'updated.csv',['new_key','code'],[[parent_new,'A2']])
    g1=import_target(db,'parent',newer,mode='full_snapshot')
    assert g1!=g0 and active_id(db)==g1
    assert ('child',actions[1]['new']) in generation_rows(db,g1)

    assert dict(connect(db).execute('SELECT * FROM target_audit WHERE id=?',(aid,)).fetchone())==audit_before
    assert dict(connect(db).execute('SELECT * FROM retirement_gate WHERE id=?',(gate,)).fetchone())==gate_before
    assert [tuple(r) for r in connect(db).execute('SELECT * FROM retirement_member WHERE gate_id=? ORDER BY old_key',(gate,))]==member_before
    assert generation_rows(db,g0)==g0_rows
    assert retirement_decision(db,aid)[1]=='retain_aliases'

def test_correction_batch_and_head_are_bound_to_target_generation(tmp_path):
    from keybridge.corrections import import_corrections,prepare_corrections,apply_corrections
    db,pid,actions,g0=seeded_target(tmp_path)
    file=tmp_path/'corrections.json'
    file.write_text(json.dumps([{'table':'parent','old_key':'p1','expected_revision':0,
                                 'after':{'id':'p1','code':'A','label':'reviewed'}}]),encoding='utf-8')
    batch=import_corrections(db,pid,file)
    batch_row=connect(db).execute('SELECT target_generation_id,generation_digest FROM correction_batch WHERE id=?',(batch,)).fetchone()
    g0_generation=connect(db).execute('SELECT digest FROM target_generation WHERE id=?',(g0,)).fetchone()[0]
    assert batch_row['target_generation_id']==g0 and batch_row['generation_digest']==g0_generation
    assert prepare_corrections(db,batch)[0][1]=='ready'
    assert apply_corrections(db,batch)=='applied'
    g1=active_id(db)
    assert g1!=g0
    head=connect(db).execute('SELECT * FROM correction_head WHERE table_name=? AND old_key=?',('parent','p1')).fetchone()
    assert head['target_generation_id']==g1 and head['generation_digest']==connect(db).execute(
        'SELECT digest FROM target_generation WHERE id=?',(g1,)).fetchone()[0]
    original=generation_rows(db,g0)
    assert 'label' not in original[('parent',actions[0]['new'])]
    assert generation_rows(db,g1)[('parent',actions[0]['new'])]['label']=='reviewed'
    assert json.loads(connect(db).execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                         ('parent',actions[0]['new'])).fetchone()[0])['label']=='reviewed'

    later=csvfile(tmp_path,'later.csv',['new_key','code'],[[actions[0]['new'],'A']])
    g2=import_target(db,'parent',later,mode='delta',base_generation=g1)
    assert g2==active_id(db) and g2!=g1
    other=tmp_path/'other.json'
    other.write_text(json.dumps([{'table':'parent','old_key':'p1','expected_revision':1,
                                  'after':{'id':'p1','code':'A','label':'second'}}]),encoding='utf-8')
    stale=import_corrections(db,pid,other)
    newer=csvfile(tmp_path,'newer.csv',['new_key','code'],[[actions[0]['new'],'A']])
    import_target(db,'parent',newer,mode='full_snapshot')
    with pytest.raises(ValueError,match='stale_target_generation'):
        prepare_corrections(db,stale)
    assert generation_rows(db,g0)==original
    assert json.loads(connect(db).execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                         ('parent',actions[0]['new'])).fetchone()[0]).get('label') is None
