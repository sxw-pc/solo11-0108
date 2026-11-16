import hashlib,json
from .store import connect
def new_key(table,natural):return table[:3].upper()+'-'+hashlib.sha256(natural.encode()).hexdigest()[:12]
def derived_key(table,natural,discriminator):
    material=natural+'|'+json.dumps(discriminator,sort_keys=True)
    return table[:3].upper()+'-'+hashlib.sha256(material.encode()).hexdigest()[:12]
def _digest(db,table):
    rows=[tuple(x) for x in db.execute('SELECT * FROM '+table+' ORDER BY 1,2')]
    return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
def group_hash(table,natural,members):
    material=json.dumps({'table':table,'natural':natural,'members':sorted([m['old_key'],m['payload']] for m in members)},sort_keys=True)
    return hashlib.sha256(material.encode()).hexdigest()
def decision_digest(db):
    adjudications=[tuple(x) for x in db.execute('SELECT table_name,natural,group_hash,decision,evidence,source_hash,contract_hash,target_hash,discriminators FROM adjudication ORDER BY table_name,natural')]
    contracts=[tuple(x) for x in db.execute('SELECT * FROM table_contract ORDER BY name')]
    return hashlib.sha256(json.dumps({'adjudications':adjudications,'contracts':contracts},default=str).encode()).hexdigest()
def adjudicate(db_path,table,natural,decision,evidence,discriminators=None):
    if decision not in ('merge','keep'):raise ValueError('invalid_decision')
    if not evidence or not str(evidence).strip():raise ValueError('missing_evidence')
    discriminators=discriminators or {}
    if not isinstance(discriminators,dict):raise ValueError('invalid_discriminator')
    db=connect(db_path)
    try:
        with db:
            members=db.execute('SELECT old_key,payload FROM source_row WHERE table_name=? AND natural=? ORDER BY old_key',(table,natural)).fetchall()
            if len(members)<2:raise ValueError('unknown_collision_group')
            payloads={m['old_key']:json.loads(m['payload']) for m in members}
            for old_key,disc in discriminators.items():
                if old_key not in payloads:raise ValueError('unknown_discriminator_row')
                for field,value in (disc or {}).items():
                    if field not in payloads[old_key] or str(payloads[old_key][field])!=str(value):raise ValueError('discriminator_mismatch')
            db.execute('INSERT OR REPLACE INTO adjudication(table_name,natural,group_hash,decision,evidence,source_hash,contract_hash,target_hash,discriminators) VALUES (?,?,?,?,?,?,?,?,?)',
                       (table,natural,group_hash(table,natural,members),decision,str(evidence),
                        _digest(db,'source_row'),_digest(db,'table_contract'),_digest(db,'target_row'),
                        json.dumps(discriminators,sort_keys=True)))
    finally:db.close()
def _map(db,table,old_key,key,version='v1'):
    db.execute('INSERT OR REPLACE INTO identity_map VALUES (?,?,?,?)',(table,old_key,key,version))
def _merge(db,table,natural,members,issues):
    contract=db.execute('SELECT pk,natural FROM table_contract WHERE name=?',(table,)).fetchone()
    ignore=set(contract['pk'].split('|'))|set(contract['natural'].split('|'))
    payloads=[json.loads(m['payload']) for m in members];base=payloads[0]
    for other in payloads[1:]:
        if any(base.get(f)!=other.get(f) for f in (set(base)|set(other))-ignore):
            issues.append(('merge_payload_conflict',table+':'+natural));return None
    for m in members:_map(db,table,m['old_key'],new_key(table,natural),'v1+merge')
    return 'merge'
def _keep(db,table,natural,members,discriminators,issues):
    payloads={m['old_key']:json.loads(m['payload']) for m in members};derived={}
    for m in members:
        disc=discriminators.get(m['old_key'])
        if not disc or any(f not in payloads[m['old_key']] for f in disc):
            issues.append(('missing_discriminator',table+':'+m['old_key']));return None
        derived[m['old_key']]=derived_key(table,natural,disc)
    if len(set(derived.values()))!=len(derived):
        issues.append(('duplicate_discriminator',table+':'+natural));return None
    for old_key,key in derived.items():_map(db,table,old_key,key,'v1+keep')
    return 'keep'
def allocate(db):
    issues=[]
    rows=list(db.execute('SELECT * FROM source_row ORDER BY table_name,natural,old_key'))
    groups={}
    for r in rows:groups.setdefault((r['table_name'],r['natural']),[]).append(r)
    contract_hash=_digest(db,'table_contract');target_hash=_digest(db,'target_row')
    for (table,natural),members in groups.items():
        if len(members)==1:
            _map(db,table,members[0]['old_key'],new_key(table,natural));continue
        adjudication=db.execute('SELECT * FROM adjudication WHERE table_name=? AND natural=?',(table,natural)).fetchone()
        resolution=None
        if adjudication is None:
            issues+=[('natural_collision',table+':'+m['old_key']) for m in members[1:]]
        elif (adjudication['group_hash']!=group_hash(table,natural,members)
              or adjudication['contract_hash']!=contract_hash
              or adjudication['target_hash']!=target_hash):
            issues.append(('adjudication_stale',table+':'+natural))
        elif adjudication['decision']=='merge':
            resolution=_merge(db,table,natural,members,issues)
        else:
            resolution=_keep(db,table,natural,members,json.loads(adjudication['discriminators']),issues)
        if resolution is None:_map(db,table,members[0]['old_key'],new_key(table,natural))
        for m in members:
            db.execute('INSERT INTO collision_group(table_name,old_key,reason,source_id,resolution) VALUES (?,?,?,?,?)',
                       (table,m['old_key'],'natural_collision',m['source_id'],resolution))
    for code,subject in issues:db.execute('INSERT INTO issue(code,subject) VALUES (?,?)',(code,subject))
    return issues
