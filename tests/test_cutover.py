import hashlib,json
import pytest
from keybridge.__main__ import demo
from keybridge.cutover import audit_target,configure_clients,observe_lookup,retirement_decision
from keybridge.store import connect


def seeded(tmp):
    demo(tmp);db=tmp/'data.db';con=connect(db)
    plan=con.execute('SELECT id FROM migration_plan ORDER BY id DESC LIMIT 1').fetchone()[0]
    return db,plan


def sha(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def test_target_reconciliation_and_cutover_require_distinct_evidence(tmp_path):
    db,plan=seeded(tmp_path)
    aid,status,details=audit_target(db,plan)
    assert status=='complete' and not details
    config=tmp_path/'clients.json';config.write_text(json.dumps([{'id':'catalog','critical':True,'table':'parent'}]),encoding='utf-8')
    configure_clients(db,config)
    assert retirement_decision(db,aid)[1]=='retain_aliases'
    con=connect(db);mapping=con.execute("SELECT old_key,new_key FROM identity_map WHERE table_name='parent'").fetchone()
    row=con.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',('parent',mapping['new_key'])).fetchone()
    observe_lookup(db,'catalog',plan,1,mapping['old_key'],mapping['new_key'],'wrong')
    assert retirement_decision(db,aid)[1]=='retain_aliases'
    with pytest.raises(ValueError,match='lookup_identity_conflict'):
        observe_lookup(db,'catalog',plan,1,mapping['old_key'],mapping['new_key'],sha(json.loads(row['payload'])))
    observe_lookup(db,'catalog',plan,2,mapping['old_key'],mapping['new_key'],sha(json.loads(row['payload'])))
    assert retirement_decision(db,aid)[1]=='retire_old_keys'
    with con:con.execute("UPDATE target_row SET payload='{}' WHERE table_name='parent'")
    assert retirement_decision(db,aid)[1]=='retain_aliases'


def test_target_drift_blocks_both_audit_and_retirement(tmp_path):
    db,plan=seeded(tmp_path)
    con=connect(db)
    with con:con.execute("UPDATE target_row SET payload='{}' WHERE table_name='parent'")
    aid,status,details=audit_target(db,plan)
    assert status=='divergent' and details
    config=tmp_path/'clients.json';config.write_text(json.dumps([{'id':'catalog','critical':True,'table':'parent'}]),encoding='utf-8')
    configure_clients(db,config)
    assert retirement_decision(db,aid)[1]=='retain_aliases'
