"""Independent target reconciliation and client key-retirement workflow."""
import hashlib
import json
from pathlib import Path

from .store import connect
from .rules import digest_rows


def _hash(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def audit_target(path, plan_id):
    db=connect(path)
    try:
        with db:
            plan=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not plan:raise ValueError('unknown_plan')
            actions=json.loads(plan['actions'])
            details=[]
            for action in actions:
                row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                               (action['table'],action['new'])).fetchone()
                actual=json.loads(row['payload']) if row else None
                if actual!=action['payload']:
                    details.append((action['table'],action['new'],'missing' if actual is None else 'payload_mismatch',
                                    json.dumps(action['payload'],sort_keys=True),json.dumps(actual,sort_keys=True)))
            mapped={(a['table'],a['new']) for a in actions}
            for edge in db.execute('SELECT child_table,child_old,parent_table,parent_new,field FROM reference_edge'):
                parent=db.execute('SELECT 1 FROM target_row WHERE table_name=? AND new_key=?',
                                  (edge['parent_table'],edge['parent_new'])).fetchone()
                child=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',
                                 (edge['child_table'],edge['child_old'])).fetchone()
                child_new=child['new_key'] if child else edge['child_old']
                if not parent:
                    details.append((edge['child_table'],child_new,'broken_reference',edge['parent_new'],None))
                    continue
                row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                               (edge['child_table'],child_new)).fetchone()
                if not row:continue
                payload=json.loads(row['payload'])
                if payload.get(edge['field'])!=edge['parent_new']:
                    details.append((edge['child_table'],child_new,'reference_not_closed',
                                    edge['parent_new'],json.dumps(payload,sort_keys=True)))
            if plan['status']!='applied':status='not_applied'
            elif plan['source_hash']!=digest_rows(db,'source_row'):status='source_changed'
            elif details:status='divergent'
            else:status='complete'
            aid=db.execute('INSERT INTO target_audit(plan_id,status,source_digest,target_digest) VALUES (?,?,?,?)',
                           (plan_id,status,digest_rows(db,'source_row'),digest_rows(db,'target_row'))).lastrowid
            db.executemany('INSERT INTO target_audit_detail VALUES (?,?,?,?,?,?)',[(aid,*x) for x in details])
            return aid,status,details
    finally:db.close()


def configure_clients(path,file):
    raw=Path(file).read_bytes(); rows=json.loads(raw)
    if not isinstance(rows,list) or not rows or len({r['id'] for r in rows})!=len(rows):
        raise ValueError('invalid_client_contract')
    sha=hashlib.sha256(raw).hexdigest();db=connect(path)
    try:
        with db:
            for row in rows:
                table=db.execute('SELECT 1 FROM table_contract WHERE name=?',(row['table'],)).fetchone()
                if not table:raise ValueError('unknown_client_table')
                item=(str(row['id']),int(bool(row['critical'])),row['table'],sha)
                old=db.execute('SELECT * FROM client_contract WHERE id=?',(row['id'],)).fetchone()
                if old and tuple(old[k] for k in ('id','critical','table_name','config_sha'))!=item:
                    raise ValueError('client_contract_changed')
                db.execute('INSERT OR IGNORE INTO client_contract VALUES (?,?,?,?)',item)
    finally:db.close()


def observe_lookup(path,client,plan_id,sequence,old_key,new_key,payload_sha):
    db=connect(path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM client_contract WHERE id=?',(client,)).fetchone():
                raise ValueError('unknown_client')
            if not db.execute('SELECT 1 FROM migration_plan WHERE id=?',(plan_id,)).fetchone():
                raise ValueError('unknown_plan')
            old=db.execute('SELECT * FROM lookup_observation WHERE client_id=? AND sequence=?',(client,sequence)).fetchone()
            expected=(plan_id,old_key,new_key,payload_sha)
            if old:
                if tuple(old[k] for k in ('plan_id','old_key','observed_new_key','observed_sha'))!=expected:
                    raise ValueError('lookup_identity_conflict')
                return old['id']
            return db.execute('INSERT INTO lookup_observation(client_id,plan_id,sequence,old_key,observed_new_key,observed_sha) VALUES (?,?,?,?,?,?)',
                              (client,plan_id,sequence,old_key,new_key,payload_sha)).lastrowid
    finally:db.close()


def retirement_decision(path,audit_id):
    db=connect(path)
    try:
        with db:
            audit=db.execute('SELECT * FROM target_audit WHERE id=?',(audit_id,)).fetchone()
            if not audit:raise ValueError('unknown_audit')
            plan=db.execute('SELECT * FROM migration_plan WHERE id=?',(audit['plan_id'],)).fetchone()
            if not db.execute('SELECT 1 FROM client_contract LIMIT 1').fetchone():
                raise ValueError('no_client_contract')
            members=[]
            for client in db.execute('SELECT * FROM client_contract ORDER BY id'):
                for mapping in db.execute('SELECT * FROM identity_map WHERE table_name=? ORDER BY old_key',(client['table_name'],)):
                    if not client['critical']:
                        members.append((client['id'],mapping['old_key'],'not_required',None));continue
                    obs=db.execute('SELECT * FROM lookup_observation WHERE client_id=? AND plan_id=? AND old_key=? ORDER BY sequence DESC LIMIT 1',
                                   (client['id'],audit['plan_id'],mapping['old_key'])).fetchone()
                    row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                   (client['table_name'],mapping['new_key'])).fetchone()
                    state=('missing_lookup' if not obs else 'missing_target' if not row else
                           'wrong_key' if obs['observed_new_key']!=mapping['new_key'] else
                           'stale_payload' if obs['observed_sha']!=_hash(json.loads(row['payload'])) else 'verified')
                    members.append((client['id'],mapping['old_key'],state,obs['id'] if obs else None))
            status='retire_old_keys' if audit['status']=='complete' and plan['status']=='applied' and audit['target_digest']==digest_rows(db,'target_row') and all(m[2] in {'verified','not_required'} for m in members) else 'retain_aliases'
            gate=db.execute('INSERT INTO retirement_gate(audit_id,status) VALUES (?,?)',(audit_id,status)).lastrowid
            db.executemany('INSERT INTO retirement_member VALUES (?,?,?,?,?)',[(gate,*m) for m in members])
            return gate,status,members
    finally:db.close()
