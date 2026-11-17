"""Issue target-bound access credentials and recheck them against live grants."""
import hashlib
import hmac
import json
import secrets

from .auth_store import connect_auth
from .authorization import _utc,evaluate

def issue(path,credential_id,request_id,issued_at,expires_at):
    start,end=_utc(issued_at),_utc(expires_at)
    if start>=end:raise ValueError('credential_window_invalid')
    db=connect_auth(path)
    try:
        with db:
            old=db.execute('SELECT * FROM issued_credential WHERE id=?',(credential_id,)).fetchone()
            if old:
                if old['request_id']!=request_id or old['issued_at']!=start.isoformat() or old['expires_at']!=end.isoformat():
                    raise ValueError('credential_identity_conflict')
                return {'credential':credential_id,'state':old['state'],'token':None}
            decision=db.execute('SELECT * FROM access_decision WHERE request_id=?',(request_id,)).fetchone()
            if not decision or decision['decision']!='allow' or decision['state']!='current':
                raise ValueError('access_allowance_required')
            request=json.loads(decision['request_json']);request['at']=start.isoformat()
            fresh=evaluate(db,request)
            if fresh['status']!='allow' or (fresh['grant_id'],fresh['grant_revision'],fresh['plan_id'],fresh['target_sha']) != \
               (decision['grant_id'],decision['grant_revision'],decision['plan_id'],decision['target_sha']):
                raise ValueError('access_decision_stale')
            grant=db.execute('SELECT valid_until FROM auth_event WHERE grant_id=? AND revision=?',
                             (fresh['grant_id'],fresh['grant_revision'])).fetchone()
            if end>_utc(grant['valid_until']):raise ValueError('credential_exceeds_grant')
            token=secrets.token_urlsafe(32)
            db.execute('INSERT INTO issued_credential VALUES (?,?,?,?,?,?,?,?,?,?)',
                       (credential_id,request_id,fresh['grant_id'],fresh['grant_revision'],fresh['plan_id'],
                        fresh['target_sha'],start.isoformat(),end.isoformat(),hashlib.sha256(token.encode()).hexdigest(),'issued'))
            return {'credential':credential_id,'state':'issued','token':token}
    finally:db.close()

def verify(path,credential_id,token,at):
    now=_utc(at);db=connect_auth(path)
    try:
        with db:
            row=db.execute('SELECT * FROM issued_credential WHERE id=?',(credential_id,)).fetchone()
            if not row:raise ValueError('unknown_credential')
            token_sha=hashlib.sha256(token.encode()).hexdigest()
            if not hmac.compare_digest(token_sha,row['token_sha']):
                status,reason='invalid','token_mismatch';fresh={}
            elif now>=_utc(row['expires_at']):
                status,reason='expired','credential_expired';fresh={}
            elif now<_utc(row['issued_at']):
                status,reason='invalid','before_issuance';fresh={}
            else:
                decision=db.execute('SELECT request_json FROM access_decision WHERE request_id=?',(row['request_id'],)).fetchone()
                request=json.loads(decision['request_json']);request['at']=now.isoformat()
                fresh=evaluate(db,request)
                if fresh['target_sha']!=row['target_sha']:
                    status,reason='stale','target_payload_changed'
                elif fresh['plan_id']!=row['plan_id']:
                    status,reason='stale','migration_plan_changed'
                elif fresh['status']=='deny' and fresh['reason']=='grant_revoked':
                    status,reason='revoked','grant_revoked'
                elif fresh['grant_revision']!=row['grant_revision']:
                    status,reason='stale','grant_version_changed'
                elif fresh['status']!='allow':
                    status,reason='stale',fresh['reason']
                else:status,reason='valid','current_grant_mapping_and_target'
            db.execute('INSERT INTO credential_check(credential_id,checked_at,status,reason,current_grant_revision,current_target_sha) VALUES (?,?,?,?,?,?)',
                       (credential_id,now.isoformat(),status,reason,fresh.get('grant_revision'),fresh.get('target_sha')))
            if status!='invalid':db.execute('UPDATE issued_credential SET state=? WHERE id=?',(status,credential_id))
            return {'credential':credential_id,'status':status,'reason':reason,
                    'grant_revision':fresh.get('grant_revision'),'target_sha':fresh.get('target_sha')}
    finally:db.close()
