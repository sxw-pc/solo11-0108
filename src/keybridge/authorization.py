"""Legacy-identity grants projected through an applied migration into access decisions."""
import hashlib
import json
from datetime import datetime,timezone
from pathlib import Path

from .auth_store import connect_auth
from .cutover import _hash
from .rules import digest_rows

def _utc(value):
    dt=datetime.fromisoformat(value.replace('Z','+00:00'))
    if dt.tzinfo is None:raise ValueError('auth_timezone_required')
    return dt.astimezone(timezone.utc)

def _canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'))

def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()

def import_grants(path,file):
    """Archive original bytes, retain out-of-order revisions, isolate same-revision conflicts."""
    raw=Path(file).read_bytes();rows=json.loads(raw)
    if not isinstance(rows,list) or not rows:raise ValueError('auth_events_required')
    source_sha=hashlib.sha256(raw).hexdigest();db=connect_auth(path)
    try:
        with db:
            old=db.execute('SELECT id FROM auth_batch WHERE source_sha=?',(source_sha,)).fetchone()
            if old:return {'batch':old['id'],'imported':0,'conflicts':0}
            batch=db.execute('INSERT INTO auth_batch(source_sha,raw) VALUES (?,?)',(source_sha,raw)).lastrowid
            imported=conflicts=0
            for row in rows:
                for key in ('grant_id','revision','table','old_key','principal','purpose','fields',
                            'valid_from','valid_until','effective_at','action'):
                    if key not in row:raise ValueError(f'auth_missing:{key}')
                if row['action'] not in ('grant','revoke') or not isinstance(row['revision'],int) or row['revision']<1:
                    raise ValueError('auth_event_invalid')
                if not isinstance(row['fields'],list) or not row['fields'] or len(set(row['fields']))!=len(row['fields']):
                    raise ValueError('auth_fields_invalid')
                start,end,effective=(_utc(row[k]) for k in ('valid_from','valid_until','effective_at'))
                if start>=end:raise ValueError('auth_window_invalid')
                if not db.execute('SELECT 1 FROM source_row WHERE table_name=? AND old_key=?',
                                  (row['table'],row['old_key'])).fetchone():raise ValueError('auth_unknown_old_identity')
                identity=(row['table'],row['old_key'],row['principal'],row['purpose'])
                head=db.execute('SELECT * FROM auth_head WHERE grant_id=?',(row['grant_id'],)).fetchone()
                if head and tuple(head[k] for k in ('table_name','old_key','principal','purpose'))!=identity:
                    raise ValueError('auth_grant_identity_conflict')
                digest=_sha(row)
                existing=db.execute('SELECT event_sha FROM auth_event WHERE grant_id=? AND revision=?',
                                    (row['grant_id'],row['revision'])).fetchone()
                if existing:
                    if existing['event_sha']!=digest:
                        db.execute('INSERT INTO auth_conflict(grant_id,revision,event_sha,reason) VALUES (?,?,?,?)',
                                   (row['grant_id'],row['revision'],digest,'same_revision_different_event'))
                        conflicts+=1
                        if head and head['revision']==row['revision']:
                            db.execute("UPDATE auth_head SET state='conflict' WHERE grant_id=?",(row['grant_id'],))
                            _stale(db,row['grant_id'])
                    continue
                db.execute('INSERT INTO auth_event VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (row['grant_id'],row['revision'],batch,*identity,_canonical(row['fields']),
                            start.isoformat(),end.isoformat(),effective.isoformat(),row['action'],digest))
                imported+=1
                if not head:
                    db.execute('INSERT INTO auth_head VALUES (?,?,?,?,?,?,?)',
                               (row['grant_id'],*identity,row['revision'],'active' if row['action']=='grant' else 'revoked'))
                elif row['revision']>head['revision']:
                    db.execute('UPDATE auth_head SET revision=?,state=? WHERE grant_id=?',
                               (row['revision'],'active' if row['action']=='grant' else 'revoked',row['grant_id']))
                    _stale(db,row['grant_id'])
            return {'batch':batch,'imported':imported,'conflicts':conflicts}
    finally:db.close()

def _stale(db,grant_id):
    db.execute("UPDATE access_decision SET state='stale' WHERE grant_id=?",(grant_id,))
    db.execute("UPDATE issued_credential SET state='needs_recheck' WHERE grant_id=?",(grant_id,))

def _grant_at(db,table,old_key,principal,purpose,when):
    heads=db.execute('SELECT * FROM auth_head WHERE table_name=? AND old_key=? AND principal=? AND purpose=?',
                     (table,old_key,principal,purpose)).fetchall()
    if any(h['state']=='conflict' for h in heads):return None,'grant_version_conflict'
    active=[];revoked=False
    for head in heads:
        events=db.execute('SELECT * FROM auth_event WHERE grant_id=? ORDER BY effective_at,revision',
                          (head['grant_id'],)).fetchall()
        selected=None
        for event in events:
            if _utc(event['effective_at'])<=when:selected=event
        if not selected:continue
        if selected['action']=='revoke':revoked=True;continue
        if _utc(selected['valid_from'])<=when<_utc(selected['valid_until']):active.append(selected)
    if len(active)>1:return None,'overlapping_active_grants'
    if not active:return None,'grant_revoked' if revoked else 'no_effective_grant'
    return active[0],None

def evaluate(db,request):
    """Compute fresh access outcome; callers persist access or credential evidence separately."""
    for key in ('table','new_key','principal','purpose','fields','at'):
        if key not in request:raise ValueError(f'access_missing:{key}')
    when=_utc(request['at'])
    fields=request['fields']
    if not isinstance(fields,list) or not fields or len(set(fields))!=len(fields):
        raise ValueError('access_fields_invalid')
    result={'status':'deny','reason':'','grant_id':None,'grant_revision':None,'plan_id':None,
            'target_sha':None,'audit_id':None,'old_key':None,'new_key':request['new_key']}
    mappings=db.execute('SELECT * FROM identity_map WHERE table_name=? AND new_key=?',
                        (request['table'],request['new_key'])).fetchall()
    if len(mappings)!=1:
        result['reason']='mapping_missing_or_ambiguous';return result
    old_key=mappings[0]['old_key'];result['old_key']=old_key
    target=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                      (request['table'],request['new_key'])).fetchone()
    if not target:result['reason']='target_missing';return result
    result['target_sha']=_hash(json.loads(target['payload']))
    plans=db.execute("SELECT * FROM migration_plan WHERE status='applied' ORDER BY id DESC").fetchall()
    for p in plans:
        if any(a['table']==request['table'] and a['old']==old_key and a['new']==request['new_key']
               for a in json.loads(p['actions'])):
            result['plan_id']=p['id'];break
    if not result['plan_id']:result['reason']='migration_not_applied';return result
    grant,problem=_grant_at(db,request['table'],old_key,request['principal'],request['purpose'],when)
    if problem:
        result['status']='uncertain' if problem in ('grant_version_conflict','overlapping_active_grants') else 'deny'
        result['reason']=problem;return result
    result.update(grant_id=grant['grant_id'],grant_revision=grant['revision'])
    if not set(fields)<=set(json.loads(grant['fields'])):
        result['reason']='field_outside_grant';return result
    audit=db.execute('SELECT * FROM target_audit WHERE plan_id=? ORDER BY id DESC LIMIT 1',
                     (result['plan_id'],)).fetchone()
    if not audit or audit['status']!='complete' or audit['target_digest']!=digest_rows(db,'target_row') \
       or audit['source_digest']!=digest_rows(db,'source_row'):
        result['status']='uncertain';result['reason']='target_audit_stale';return result
    result['audit_id']=audit['id'];result['status']='allow';result['reason']='mapped_grant_and_target_verified'
    return result

def check_access(path,request):
    request_id=request.get('request_id')
    if not request_id:raise ValueError('access_request_id_required')
    db=connect_auth(path)
    try:
        with db:
            fingerprint=_sha(request)
            old=db.execute('SELECT * FROM access_decision WHERE request_id=?',(request_id,)).fetchone()
            if old:
                if old['request_sha']!=fingerprint:raise ValueError('access_request_identity_conflict')
                return {'request':request_id,'decision':old['decision'],'reason':old['reason'],
                        'state':old['state'],'grant_revision':old['grant_revision']}
            result=evaluate(db,request)
            db.execute('INSERT INTO access_decision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id,fingerprint,_canonical(request),result['status'],result['reason'],'current',
                        result['grant_id'],result['grant_revision'],result['plan_id'],result['target_sha'],
                        result['audit_id'],result['old_key'],result['new_key']))
            return {'request':request_id,'decision':result['status'],'reason':result['reason'],
                    'state':'current','grant_revision':result['grant_revision']}
    finally:db.close()
