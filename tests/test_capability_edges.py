import csv,json
import pytest
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows
from keybridge.rules import plan,apply,rollback
from keybridge.report import write
from keybridge.store import connect
def csvfile(tmp,name,header,rows):
    p=tmp/name
    with p.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(header);w.writerows(rows)
    return p
def setup(tmp,parents=None,children=None):
    db=tmp/'x.db';p=tmp/'contract.json'
    p.write_text(json.dumps({'tables':[{'name':'parent','pk':'id','natural':'code'},
      {'name':'child','pk':'id','natural':'code','refs':[{'field':'parent_id','table':'parent'}]}]}))
    compile_contract(p,db)
    import_rows(db,'parent',csvfile(tmp,'p.csv',['id','code'],parents or [['p1','A']]))
    import_rows(db,'child',csvfile(tmp,'c.csv',['id','code','parent_id'],children or [['c1','C','p1']]))
    return db
def test_contract_topology_and_missing_column(tmp_path):
    db=setup(tmp_path)
    assert [r[0] for r in connect(db).execute('SELECT name FROM table_contract ORDER BY position')]==['parent','child']
    with pytest.raises(ValueError,match='missing_column'):
        import_rows(db,'parent',csvfile(tmp_path,'bad.csv',['id'],[['x']]))
def test_source_bytes_and_natural_key_casefold(tmp_path):
    db=setup(tmp_path,parents=[['p1','  A  ']])
    c=connect(db)
    assert c.execute("SELECT natural FROM source_row WHERE table_name='parent'").fetchone()[0]=='a'
    assert c.execute('SELECT raw FROM source_file ORDER BY id DESC LIMIT 1').fetchone()[0]==(tmp_path/'c.csv').read_bytes()
def test_mapping_independent_of_csv_order(tmp_path):
    db=setup(tmp_path,parents=[['p2','B'],['p1','A']]);_,_,actions=plan(db)
    keys={a['old']:a['new'] for a in actions if a['table']=='parent'}
    assert keys['p1']!=keys['p2'] and list(keys)==['p1','p2']
def test_parent_child_ref_and_target_reuse(tmp_path):
    db=setup(tmp_path);_,_,actions=plan(db)
    c=connect(db)
    with c:c.execute('INSERT INTO target_row VALUES (?,?,?)',('parent',actions[0]['new'],json.dumps(actions[0]['payload'],sort_keys=True)))
    _,status,actions=plan(db)
    assert status=='ready' and actions[0]['kind']=='reuse' and actions[1]['payload']['parent_id']==actions[0]['new']
def test_plan_is_stable_and_apply_records_steps(tmp_path):
    db=setup(tmp_path);pid,status,actions=plan(db)
    assert status=='ready' and plan(db)[2]==actions
    apply(db,pid)
    c=connect(db)
    assert c.execute('SELECT COUNT(*) FROM apply_step WHERE plan_id=?',(pid,)).fetchone()[0]==2
def test_report_count_conservation(tmp_path):
    db=setup(tmp_path);pid,_,_=plan(db)
    data=json.loads(write(db,pid,tmp_path/'r').read_text(encoding='utf-8'))
    assert data['counts']['parent']=={'input':1,'insert':1,'reuse':0,'merged':0,'blocked':0,'isolated':0,'conserved':True}
    assert data['sources'][0]['sha'] and (tmp_path/'r.md').exists()
