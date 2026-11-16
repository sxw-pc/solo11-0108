"""Independent target reconciliation and client key-retirement workflow."""
import hashlib
import json
from pathlib import Path

from .store import connect
from .rules import digest_rows
from .corrections import _history_rows, _contains, _interval_of, _instant


def _hash(payload):
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def _cutoff_filter(rows, valid_cutoff, recorded_cutoff):
    if recorded_cutoff is not None:
        if isinstance(recorded_cutoff, int):
            rows = [row for row in rows if row['recorded_seq'] <= recorded_cutoff]
        else:
            cutoff = _instant(recorded_cutoff)
            rows = [row for row in rows if str(row['recorded_at']) <= cutoff]
    if valid_cutoff is not None:
        point = (1, str(valid_cutoff))
        rows = [row for row in rows if _contains(_interval_of(row), point)]
    return rows


def _bitemporal_expected(db, table, old_key, action_payload, valid_cutoff, recorded_cutoff):
    """Expected target payload at a pair of bitemporal cutoffs.

    Starts from the frozen migration payload and overlays only fields that a
    correction declares, using corrections that are both business-valid at
    ``valid_cutoff`` and recorded at or before ``recorded_cutoff``. Target-only
    fields (fields no correction ever declares) are deliberately outside the
    compared field set; they belong to the target team.
    """
    rows = _cutoff_filter(_history_rows(db, table, old_key), valid_cutoff, recorded_cutoff)
    expected = dict(action_payload)
    owners = {}
    for row in sorted(rows, key=lambda item: (item['recorded_seq'], item['id'])):
        projected = json.loads(row['projected_payload'])
        for field in json.loads(row['declared_fields']):
            if field in projected:
                expected[field] = projected[field]
            else:
                expected.pop(field, None)
            owners[field] = row
    return expected, owners


def _managed_fields(action_payload, owners):
    """Fields this audit's expected reconstruction can speak about.

    The migration projection is always compared; a correction widens the
    compared field set only when it is effective at the audit's business
    cutoff and recorded by the receipt cutoff (i.e. when it contributed to
    ``owners``). A correction from another business interval therefore cannot
    retroactively widen a historical audit's scope.
    """
    return set(action_payload) | set(owners)


def _field_evidence(row):
    if row is None:
        return {'value_source': 'migration_plan'}
    return {'value_source': 'correction_history',
            'history_id': row['id'], 'revision': row['revision'],
            'recorded_seq': row['recorded_seq'], 'recorded_at': row['recorded_at'],
            'valid_from': row['valid_from'], 'valid_to': row['valid_to']}


def audit_target(path, plan_id, valid_cutoff=None, recorded_cutoff=None,
                 business_at=None, recorded_by=None):
    """Reconcile target rows against the migration plan and correction history.

    With no cutoffs the audit keeps the original semantics: compare against
    the current ``correction_head`` (or the migration payload when no
    correction exists).

    With ``valid_cutoff`` and/or ``recorded_cutoff`` the expected values are
    reconstructed bitemporally: start from the frozen migration payload and
    overlay only fields declared by corrections that are business-valid at
    ``valid_cutoff`` and recorded at or before ``recorded_cutoff``. The
    comparison is restricted to fields the migration and corrections manage,
    so target-only annotations never make a historical audit diverge.

    Every audited row is bound to both cutoffs and to the correction revision
    (and history row) that produced each expected field. Evidence is appended
    to ``target_audit_evidence`` and never updated, so a later correction
    cannot rewrite the conclusion of an existing audit.
    """
    valid_cutoff=business_at if business_at is not None else valid_cutoff
    recorded_cutoff=recorded_by if recorded_by is not None else recorded_cutoff
    bitemporal=valid_cutoff is not None or recorded_cutoff is not None
    db=connect(path)
    try:
        with db:
            plan=db.execute('SELECT * FROM migration_plan WHERE id=?',(plan_id,)).fetchone()
            if not plan:raise ValueError('unknown_plan')
            actions=json.loads(plan['actions'])
            details=[]
            evidence=[]
            for action in actions:
                row=db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                               (action['table'],action['new'])).fetchone()
                actual=json.loads(row['payload']) if row else None
                if bitemporal:
                    expected,owners=_bitemporal_expected(db,action['table'],action['old'],
                                                         action['payload'],valid_cutoff,recorded_cutoff)
                    managed=_managed_fields(action['payload'],owners)
                    if actual is None:
                        mismatch=sorted(managed)
                        details.append((action['table'],action['new'],'missing',
                                        json.dumps({field:expected.get(field) for field in mismatch},sort_keys=True),
                                        None))
                        verdict='missing'
                    else:
                        mismatch=sorted(field for field in managed if expected.get(field)!=actual.get(field))
                        if mismatch:
                            details.append((action['table'],action['new'],'payload_mismatch',
                                            json.dumps({field:expected.get(field) for field in mismatch},sort_keys=True),
                                            json.dumps({field:actual.get(field) for field in mismatch},sort_keys=True)))
                        verdict='divergent' if mismatch else 'match'
                    field_evidence={field:_field_evidence(owners.get(field)) for field in sorted(managed)}
                    used=[source for source in owners.values()]
                else:
                    head=db.execute('SELECT target_payload,history_id FROM correction_head WHERE table_name=? AND old_key=?',
                                    (action['table'],action['old'])).fetchone()
                    expected=json.loads(head['target_payload']) if head else action['payload']
                    mismatch=actual!=expected
                    if mismatch:
                        details.append((action['table'],action['new'],
                                        'missing' if actual is None else 'payload_mismatch',
                                        json.dumps(expected,sort_keys=True),
                                        json.dumps(actual,sort_keys=True) if actual is not None else None))
                    verdict='missing' if actual is None else 'divergent' if mismatch else 'match'
                    field_evidence={}
                    history=None
                    if head and head['history_id']:
                        history=db.execute('SELECT * FROM correction_history WHERE id=?',(head['history_id'],)).fetchone()
                        if history:
                            field_evidence={field:_field_evidence(history)
                                            for field in json.loads(history['declared_fields'])}
                    used=[history] if history else []
                latest=max(used,key=lambda item:(item['recorded_seq'],item['id'])) if used else None
                evidence.append((action['table'],action['new'],action['old'],
                                 'correction_history' if latest else 'migration_plan',
                                 latest['revision'] if latest else None,
                                 latest['id'] if latest else None,
                                 latest['recorded_seq'] if latest else None,
                                 latest['valid_from'] if latest else None,
                                 latest['valid_to'] if latest else None,
                                 latest['recorded_at'] if latest else None,
                                 valid_cutoff,recorded_cutoff,
                                 json.dumps(expected,sort_keys=True),
                                 json.dumps(actual,sort_keys=True) if actual is not None else None,
                                 _hash(expected),_hash(actual) if actual is not None else None,
                                 json.dumps(field_evidence,sort_keys=True),verdict))
            mapped={(a['table'],a['new']) for a in actions}
            for edge in db.execute('SELECT child_table,child_old,parent_table,parent_new FROM reference_edge'):
                parent=db.execute('SELECT 1 FROM target_row WHERE table_name=? AND new_key=?',
                                  (edge['parent_table'],edge['parent_new'])).fetchone()
                if not parent:
                    child=db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',
                                     (edge['child_table'],edge['child_old'])).fetchone()
                    details.append((edge['child_table'],child['new_key'] if child else edge['child_old'],
                                    'broken_reference',edge['parent_new'],None))
            if plan['status']!='applied':status='not_applied'
            elif plan['source_hash']!=digest_rows(db,'source_row'):status='source_changed'
            elif details:status='divergent'
            else:status='complete'
            aid=db.execute('INSERT INTO target_audit(plan_id,status,source_digest,target_digest,valid_cutoff,recorded_cutoff) VALUES (?,?,?,?,?,?)',
                           (plan_id,status,digest_rows(db,'source_row'),digest_rows(db,'target_row'),
                            valid_cutoff,recorded_cutoff)).lastrowid
            db.executemany('INSERT INTO target_audit_detail VALUES (?,?,?,?,?,?)',[(aid,*x) for x in details])
            db.executemany('INSERT INTO target_audit_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           [(aid,*item) for item in evidence])
            return aid,status,details
    finally:db.close()


def audit_evidence(path, audit_id):
    """Stored, immutable evidence for one audit (never rewritten by corrections)."""
    db=connect(path)
    try:
        audit=db.execute('SELECT * FROM target_audit WHERE id=?',(audit_id,)).fetchone()
        if not audit:raise ValueError('unknown_audit')
        rows=[]
        for row in db.execute('SELECT * FROM target_audit_evidence WHERE audit_id=? ORDER BY table_name,new_key',(audit_id,)):
            item={key:row[key] for key in row.keys()}
            item['field_evidence']=json.loads(item.pop('evidence') or '{}')
            rows.append(item)
        return {'audit':dict(audit),'evidence':rows,
                'details':[dict(row) for row in db.execute('SELECT * FROM target_audit_detail WHERE audit_id=? ORDER BY table_name,new_key',(audit_id,))]}
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
