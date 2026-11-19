"""Training eligibility and serving parity from independently captured feature evidence."""

import json
from datetime import datetime, timedelta

from .feature_store import canonical, connect, rows, sha, source, timestamp


def _read(file):
    from pathlib import Path
    return Path(file).read_bytes(), json.loads(Path(file).read_text(encoding='utf-8'))


def register_contract(db_path, file):
    raw, doc = _read(file)
    features = doc['features']
    if not doc['id'] or len(features) < 2 or len({f['name'] for f in features}) != len(features):
        raise ValueError('invalid_feature_contract')
    for feature in features:
        if not isinstance(feature.get('ttl_hours'), int) or feature['ttl_hours'] <= 0:
            raise ValueError('invalid_ttl')
        if not isinstance(feature.get('required'), bool):
            raise ValueError('invalid_required_flag')
    if not isinstance(doc.get('min_eligible'), int) or doc['min_eligible'] < 1:
        raise ValueError('invalid_min_eligible')
    with connect(db_path) as db, db:
        old = db.execute('SELECT sha FROM feature_contract WHERE id=?', (doc['id'],)).fetchone()
        if old:
            if old['sha'] != sha(raw): raise ValueError('contract_id_conflict')
            return {'contract_id': doc['id'], 'replayed': True}
        db.execute('INSERT INTO feature_contract VALUES(?,?,?,?,?)',
                   (doc['id'],raw,sha(raw),canonical(features),doc['min_eligible']))
    return {'contract_id':doc['id'], 'replayed':False}


def import_observations(db_path, file):
    raw, doc = _read(file)
    if not isinstance(doc.get('observations'), list) or not doc['observations']:
        raise ValueError('observations_required')
    with connect(db_path) as db, db:
        source_id, _ = source(db,'observations',file)
        for obs in doc['observations']:
            oid, rev = obs['observation_id'], obs['revision']
            if not oid or not isinstance(rev,int) or rev < 1 or not obs['entity_id']:
                raise ValueError('invalid_observation_identity')
            event_at, available_at = timestamp(obs['event_at']),timestamp(obs['available_at'])
            if available_at < event_at: raise ValueError('available_before_event')
            action = obs.get('action','upsert')
            if action not in ('upsert','retract'): raise ValueError('invalid_action')
            if action == 'upsert' and 'value' not in obs: raise ValueError('value_required')
            old = db.execute('SELECT * FROM feature_observation WHERE observation_id=? AND revision=?',(oid,rev)).fetchone()
            value = canonical(obs['value']) if action == 'upsert' else None
            record=(oid,rev,obs['entity_id'],obs['feature_name'],event_at,available_at,value,action)
            if old:
                prior=tuple(old[k] for k in ('observation_id','revision','entity_id','feature_name','event_at','available_at','value_json','action'))
                if prior != record: raise ValueError('observation_revision_conflict')
                continue
            latest=db.execute('SELECT MAX(revision) AS r,MAX(available_at) AS a FROM feature_observation WHERE observation_id=?',(oid,)).fetchone()
            if latest['r'] is not None and (rev != latest['r']+1 or available_at <= latest['a']):
                raise ValueError('revision_order_conflict')
            db.execute('INSERT INTO feature_observation VALUES(?,?,?,?,?,?,?,?,?)',(*record,source_id))
    return {'source_sha':sha(raw),'count':len(doc['observations'])}


def import_labels(db_path,file):
    raw,doc=_read(file)
    if not isinstance(doc.get('labels'),list) or not doc['labels']:
        raise ValueError('labels_required')
    with connect(db_path) as db, db:
        source_id,_=source(db,'labels',file)
        for label in doc['labels']:
            prediction,outcome,known=map(timestamp,(label['prediction_at'],label['outcome_at'],label['known_at']))
            if not prediction < outcome <= known: raise ValueError('label_time_order')
            record=(label['label_id'],label['entity_id'],prediction,outcome,known,canonical(label['outcome']))
            old=db.execute('SELECT * FROM feature_label WHERE label_id=?',(label['label_id'],)).fetchone()
            if old:
                prior=tuple(old[k] for k in ('label_id','entity_id','prediction_at','outcome_at','known_at','outcome_json'))
                if prior != record: raise ValueError('label_id_conflict')
                continue
            db.execute('INSERT INTO feature_label VALUES(?,?,?,?,?,?,?)',(*record,source_id))
    return {'source_sha':sha(raw),'count':len(doc['labels'])}


def _feature_at(db,entity,name,at,ttl_hours):
    eligible=rows(db,'''SELECT * FROM feature_observation WHERE entity_id=? AND feature_name=?
      AND event_at<=? AND available_at<=? ORDER BY event_at DESC,observation_id,revision DESC''',(entity,name,at,at))
    latest={}
    for item in eligible:
        key=item['observation_id']
        if key not in latest or item['revision']>latest[key]['revision']:latest[key]=item
    ordered=sorted(latest.values(),key=lambda x:(x['event_at'],x['available_at'],x['observation_id']),reverse=True)
    if not ordered:return None,'missing'
    chosen=ordered[0]
    if chosen['action']=='retract':return None,'retracted'
    age=datetime.fromisoformat(at.replace('Z','+00:00'))-datetime.fromisoformat(chosen['event_at'].replace('Z','+00:00'))
    if age>timedelta(hours=ttl_hours):return None,'expired'
    return chosen,None


def build_training(db_path,contract_id,freeze_at):
    freeze_at=timestamp(freeze_at)
    with connect(db_path) as db, db:
        contract=db.execute('SELECT * FROM feature_contract WHERE id=?',(contract_id,)).fetchone()
        if not contract:raise ValueError('unknown_contract')
        features=json.loads(contract['features_json'])
        labels=rows(db,'SELECT * FROM feature_label ORDER BY label_id')
        if not labels:raise ValueError('no_labels')
        output=[]
        for label in labels:
            values,evidence,reasons={}, {}, []
            if label['known_at']>freeze_at:reasons.append('outcome_not_known')
            for feature in features:
                found,reason=_feature_at(db,label['entity_id'],feature['name'],label['prediction_at'],feature['ttl_hours'])
                values[feature['name']]=json.loads(found['value_json']) if found else None
                evidence[feature['name']]={'observation_id':found['observation_id'],'revision':found['revision']} if found else {'reason':reason}
                if reason and feature['required']:reasons.append(f"{feature['name']}:{reason}")
            output.append((label,'eligible' if not reasons else 'excluded',','.join(reasons),values,evidence))
        eligible=sum(status=='eligible' for _,status,*_ in output)
        status='ready' if eligible>=contract['min_eligible'] else 'blocked'
        manifest=sha(canonical([{'label':x[0]['label_id'],'status':x[1],'values':x[3],'evidence':x[4]} for x in output]).encode())
        cur=db.execute('INSERT INTO feature_training(contract_id,freeze_at,status,eligible_count,excluded_count,manifest_sha) VALUES(?,?,?,?,?,?)',
                       (contract_id,freeze_at,status,eligible,len(output)-eligible,manifest))
        for label,row_status,reason,values,evidence in output:
            db.execute('INSERT INTO feature_training_row VALUES(?,?,?,?,?,?,?,?)',
                       (cur.lastrowid,label['label_id'],label['entity_id'],label['prediction_at'],row_status,reason,canonical(values),canonical(evidence)))
        return {'build_id':cur.lastrowid,'status':status,'eligible':eligible,'excluded':len(output)-eligible,'manifest_sha':manifest}


def import_capture(db_path,file):
    raw,doc=_read(file)
    at=timestamp(doc['prediction_at'])
    with connect(db_path) as db, db:
        if not db.execute('SELECT 1 FROM feature_contract WHERE id=?',(doc['contract_id'],)).fetchone():raise ValueError('unknown_contract')
        source_id,_=source(db,'capture',file)
        old=db.execute('SELECT source_id FROM feature_serving_capture WHERE id=?',(doc['capture_id'],)).fetchone()
        if old:
            prior=db.execute('SELECT sha FROM feature_source WHERE id=?',(old['source_id'],)).fetchone()['sha']
            if prior != sha(raw):raise ValueError('capture_id_conflict')
            return {'capture_id':doc['capture_id'],'replayed':True}
        db.execute('INSERT INTO feature_serving_capture VALUES(?,?,?,?)',(doc['capture_id'],doc['contract_id'],at,source_id))
        for value in doc['values']:
            db.execute('INSERT INTO feature_serving_value VALUES(?,?,?,?,?,?)',
                       (doc['capture_id'],value['entity_id'],value['feature_name'],canonical(value.get('value')),
                        value.get('observation_id'),value.get('revision')))
    return {'capture_id':doc['capture_id'],'replayed':False}


def check_parity(db_path,build_id,capture_id):
    with connect(db_path) as db, db:
        build=db.execute('SELECT * FROM feature_training WHERE id=?',(build_id,)).fetchone()
        capture=db.execute('SELECT * FROM feature_serving_capture WHERE id=?',(capture_id,)).fetchone()
        if not build or not capture:raise ValueError('unknown_build_or_capture')
        if build['contract_id'] != capture['contract_id']:raise ValueError('contract_mismatch')
        selected=rows(db,"SELECT * FROM feature_training_row WHERE build_id=? AND prediction_at=? AND status='eligible'",(build_id,capture['prediction_at']))
        if not selected:raise ValueError('no_matching_prediction_cohort')
        observed={(x['entity_id'],x['feature_name']):x for x in rows(db,'SELECT * FROM feature_serving_value WHERE capture_id=?',(capture_id,))}
        mismatch=[]
        for row in selected:
            evidence=json.loads(row['evidence_json']); values=json.loads(row['values_json'])
            for name,expected in values.items():
                actual=observed.get((row['entity_id'],name))
                if not actual or json.loads(actual['value_json']) != expected or (actual['observation_id'],actual['revision']) != (evidence[name].get('observation_id'),evidence[name].get('revision')):
                    mismatch.append({'label_id':row['label_id'],'feature':name,'reason':'missing_or_different_serving_evidence'})
        status='matched' if not mismatch else 'mismatch'
        cur=db.execute('INSERT INTO feature_parity(build_id,capture_id,status,mismatch_json,checked_at) VALUES(?,?,?,?,datetime("now"))',
                       (build_id,capture_id,status,canonical(mismatch)))
        return {'parity_id':cur.lastrowid,'status':status,'mismatch':mismatch}


def release(db_path,build_id,parity_id):
    with connect(db_path) as db, db:
        build=db.execute('SELECT * FROM feature_training WHERE id=?',(build_id,)).fetchone()
        parity=db.execute('SELECT * FROM feature_parity WHERE id=?',(parity_id,)).fetchone()
        if not build or not parity or parity['build_id']!=build_id or build['status']!='ready' or parity['status']!='matched':
            raise ValueError('release_gate_blocked')
        if db.execute('SELECT 1 FROM feature_release WHERE build_id=?',(build_id,)).fetchone():raise ValueError('already_released')
        cur=db.execute('INSERT INTO feature_release(build_id,parity_id,manifest_sha,released_at) VALUES(?,?,?,datetime("now"))',
                       (build_id,parity_id,build['manifest_sha']))
        return {'release_id':cur.lastrowid,'manifest_sha':build['manifest_sha']}


def report(db_path,build_id):
    with connect(db_path) as db:
        build=db.execute('SELECT * FROM feature_training WHERE id=?',(build_id,)).fetchone()
        if not build:raise ValueError('unknown_build')
        return {'build':dict(build),'rows':rows(db,'SELECT * FROM feature_training_row WHERE build_id=? ORDER BY label_id',(build_id,)),
                'parity':rows(db,'SELECT * FROM feature_parity WHERE build_id=? ORDER BY id',(build_id,)),
                'release':rows(db,'SELECT * FROM feature_release WHERE build_id=?',(build_id,))}
