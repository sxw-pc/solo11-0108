"""Authorization evidence report without raw credential secrets."""
import json
from pathlib import Path

from .auth_store import connect_auth

def report(path,out=None):
    db=connect_auth(path)
    try:
        result={
          'batches':[dict(x) for x in db.execute('SELECT id,source_sha,created_at FROM auth_batch ORDER BY id')],
          'grants':[dict(x) for x in db.execute('SELECT * FROM auth_head ORDER BY grant_id')],
          'events':[dict(x) for x in db.execute('SELECT grant_id,revision,action,effective_at,event_sha FROM auth_event ORDER BY grant_id,revision')],
          'conflicts':[dict(x) for x in db.execute('SELECT grant_id,revision,event_sha,reason FROM auth_conflict ORDER BY id')],
          'access':[dict(x) for x in db.execute('SELECT request_id,decision,reason,state,grant_id,grant_revision,plan_id,audit_id,old_key,new_key FROM access_decision ORDER BY request_id')],
          'credentials':[dict(x) for x in db.execute('SELECT id,request_id,grant_id,grant_revision,plan_id,issued_at,expires_at,state FROM issued_credential ORDER BY id')],
          'checks':[dict(x) for x in db.execute('SELECT * FROM credential_check ORDER BY id')],
        }
    finally:db.close()
    if out:
        dest=Path(out);dest.parent.mkdir(parents=True,exist_ok=True)
        temp=dest.with_name(dest.name+'.pending')
        temp.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        temp.replace(dest)
    return result
