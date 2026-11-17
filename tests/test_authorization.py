import json
from pathlib import Path

import pytest

from keybridge.__main__ import demo
from keybridge.auth_report import report
from keybridge.authorization import check_access,import_grants
from keybridge.credentials import issue,verify
from keybridge.corrections import import_corrections,prepare_corrections,apply_corrections
from keybridge.cutover import audit_target
from keybridge.rules import rollback
from keybridge.store import connect

FIX=Path(__file__).resolve().parent/'fixtures'

def ready(tmp_path):
    demo(tmp_path);db=tmp_path/'data.db'
    con=connect(db)
    plan=con.execute('SELECT max(id) FROM migration_plan').fetchone()[0]
    mapped=con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p1'").fetchone()[0]
    con.close()
    audit_target(db,plan)
    request=json.loads((FIX/'auth_request_template.json').read_text(encoding='utf-8'))
    request['new_key']=mapped
    return db,plan,request

def test_legacy_grant_inherits_only_to_mapped_audited_target(tmp_path):
    db,plan,request=ready(tmp_path)
    assert import_grants(db,FIX/'auth_normal.json')['imported']==1
    assert import_grants(db,FIX/'auth_normal.json')['imported']==0
    assert check_access(db,request)['decision']=='allow'
    issued=issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')
    assert issued['token'] and verify(db,'cred-1',issued['token'],'2026-06-02T00:00:00Z')['status']=='valid'
    assert issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')['token'] is None
    evidence=report(db,tmp_path/'auth-report.json')
    assert evidence['access'][0]['plan_id']==plan
    assert issued['token'] not in json.dumps(evidence)

def test_revoke_and_regrant_change_access_but_do_not_reactivate_old_token(tmp_path):
    db,_,request=ready(tmp_path);import_grants(db,FIX/'auth_normal.json')
    check_access(db,request)
    token=issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')['token']
    import_grants(db,FIX/'auth_revoke.json')
    assert report(db)['access'][0]['state']=='stale'
    assert verify(db,'cred-1',token,'2026-08-01T00:00:00Z')['status']=='revoked'
    denied={**request,'request_id':'access-2','at':'2026-08-01T00:00:00Z'}
    assert check_access(db,denied)['reason']=='grant_revoked'
    import_grants(db,FIX/'auth_regrant.json')
    assert verify(db,'cred-1',token,'2026-09-02T00:00:00Z')['reason']=='grant_version_changed'
    recovered={**request,'request_id':'access-3','at':'2026-09-02T00:00:00Z'}
    assert check_access(db,recovered)['decision']=='allow'
    new=issue(db,'cred-2','access-3','2026-09-02T01:00:00Z','2026-12-01T00:00:00Z')
    assert verify(db,'cred-2',new['token'],'2026-09-03T00:00:00Z')['status']=='valid'

def test_same_revision_conflict_and_field_scope_are_separate_decisions(tmp_path):
    db,_,request=ready(tmp_path);import_grants(db,FIX/'auth_normal.json')
    wider={**request,'request_id':'wide','fields':['code','label']}
    assert check_access(db,wider)['reason']=='field_outside_grant'
    assert import_grants(db,FIX/'auth_conflict.json')['conflicts']==1
    later={**request,'request_id':'conflicted'}
    assert check_access(db,later)=={'request':'conflicted','decision':'uncertain',
                                    'reason':'grant_version_conflict','state':'current','grant_revision':None}
    with pytest.raises(ValueError,match='access_allowance_required'):
        issue(db,'cred-no','conflicted','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')

def test_target_correction_requires_new_audit_and_new_credential(tmp_path):
    db,plan,request=ready(tmp_path);import_grants(db,FIX/'auth_normal.json')
    check_access(db,request)
    token=issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')['token']
    batch=import_corrections(db,plan,FIX/'correction_normal.json')
    assert all(x[1]=='ready' for x in prepare_corrections(db,batch))
    assert apply_corrections(db,batch)=='applied'
    assert verify(db,'cred-1',token,'2026-06-02T00:00:00Z')['reason']=='target_payload_changed'
    pending={**request,'request_id':'access-after-correction','at':'2026-06-02T00:00:00Z'}
    assert check_access(db,pending)['reason']=='target_audit_stale'
    audit_target(db,plan)
    fresh={**request,'request_id':'access-after-audit','at':'2026-06-02T00:00:00Z'}
    assert check_access(db,fresh)['decision']=='allow'
    new=issue(db,'cred-2','access-after-audit','2026-06-02T01:00:00Z','2026-12-01T00:00:00Z')
    assert verify(db,'cred-2',new['token'],'2026-06-03T00:00:00Z')['status']=='valid'

def test_mapping_rollback_and_wrong_token_invalidate_access(tmp_path):
    db,plan,request=ready(tmp_path);import_grants(db,FIX/'auth_normal.json')
    check_access(db,request)
    token=issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-12-01T00:00:00Z')['token']
    assert verify(db,'cred-1','wrong','2026-06-02T00:00:00Z')['status']=='invalid'
    rollback(db,plan)
    assert verify(db,'cred-1',token,'2026-06-02T00:00:00Z')['status']=='stale'
    after={**request,'request_id':'after-rollback'}
    assert check_access(db,after)['reason']=='target_missing'

def test_late_import_keeps_historical_access_time_but_revokes_later_time(tmp_path):
    db,_,request=ready(tmp_path)
    import_grants(db,FIX/'auth_revoke.json')
    import_grants(db,FIX/'auth_normal.json')
    assert check_access(db,request)['decision']=='allow'
    later={**request,'request_id':'access-later','at':'2026-08-01T00:00:00Z'}
    assert check_access(db,later)['reason']=='grant_revoked'

def test_access_request_identity_and_credential_expiry_are_frozen(tmp_path):
    db,_,request=ready(tmp_path);import_grants(db,FIX/'auth_normal.json')
    assert check_access(db,request)['decision']=='allow'
    with pytest.raises(ValueError,match='access_request_identity_conflict'):
        check_access(db,{**request,'fields':['label']})
    token=issue(db,'cred-1','access-1','2026-06-01T01:00:00Z','2026-06-03T00:00:00Z')['token']
    assert verify(db,'cred-1',token,'2026-06-03T00:00:00Z')['status']=='expired'
