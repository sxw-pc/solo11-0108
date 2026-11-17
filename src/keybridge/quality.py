"""Independent pre-migration data quality isolation, reviewed repair and release."""

import csv
import hashlib
import json
from contextlib import closing
from pathlib import Path

from .store import connect


def _canon(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def register_contract(db_path, path):
    raw = Path(path).read_bytes()
    item = json.loads(raw)
    ident = str(item['id'])
    table = str(item['table'])
    identity = str(item['identity_field'])
    required = item['required_fields']
    numeric = item.get('numeric_ranges', {})
    if not ident or not table or not identity or not isinstance(required, list) or identity not in required:
        raise ValueError('invalid quality contract')
    if not all(isinstance(x, str) and x for x in required):
        raise ValueError('required fields must be nonempty strings')
    if len(set(required)) != len(required):
        raise ValueError('duplicate required field')
    for field, bounds in numeric.items():
        if not isinstance(bounds, list) or len(bounds) != 2 or bounds[0] > bounds[1]:
            raise ValueError(f'invalid numeric range for {field}')
    with closing(connect(db_path)) as db, db:
        old = db.execute('SELECT contract_sha FROM quality_contract WHERE id=?', (ident,)).fetchone()
        if old:
            if old['contract_sha'] != _sha(raw):
                raise ValueError('quality contract id reused with changed source bytes')
            return {'contract': ident, 'idempotent': True}
        db.execute('INSERT INTO quality_contract VALUES(?,?,?,?,?,?,?)',
                   (ident, table, identity, _canon(required), _canon(numeric), _sha(raw), raw))
    return {'contract': ident, 'table': table}


def import_batch(db_path, contract_id, path):
    raw = Path(path).read_bytes()
    items = json.loads(raw)
    if not isinstance(items, list) or not items:
        raise ValueError('quality batch must be nonempty JSON list')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get('payload'), dict) or not item.get('source_id'):
            raise ValueError('quality item requires source_id and payload object')
        if item['source_id'] in seen:
            raise ValueError('duplicate source_id within batch')
        seen.add(item['source_id'])
    with closing(connect(db_path)) as db, db:
        if not db.execute('SELECT 1 FROM quality_contract WHERE id=?', (contract_id,)).fetchone():
            raise ValueError('unknown quality contract')
        old = db.execute('SELECT id,contract_id FROM quality_batch WHERE source_sha=?', (_sha(raw),)).fetchone()
        if old:
            if old['contract_id'] != contract_id:
                raise ValueError('same batch source assigned to another contract')
            return {'batch_id': old['id'], 'idempotent': True}
        batch_id = db.execute('INSERT INTO quality_batch(contract_id,source_sha,raw,status) VALUES(?,?,?,?)',
                              (contract_id, _sha(raw), raw, 'imported')).lastrowid
        for row_no, item in enumerate(items, 1):
            payload = _canon(item['payload'])
            db.execute('INSERT INTO quality_item VALUES(?,?,?,?,?,?,?,?)',
                       (batch_id, row_no, str(item['source_id']), payload, payload,
                        _sha(payload.encode()), 'pending', 'not evaluated'))
    return {'batch_id': batch_id, 'rows': len(items)}


def _judge(contract, rows):
    identity_field = contract['identity_field']
    required = json.loads(contract['required_json'])
    numeric = json.loads(contract['numeric_json'])
    keys = {}
    for row in rows:
        payload = json.loads(row['current_json'])
        key = payload.get(identity_field)
        if key is not None and str(key).strip():
            keys.setdefault(str(key), []).append(row['row_no'])
    result = []
    for row in rows:
        payload = json.loads(row['current_json'])
        defects = []
        for field in required:
            if field not in payload or payload[field] is None or str(payload[field]).strip() == '':
                defects.append(f'missing:{field}')
        key = payload.get(identity_field)
        if key is not None and len(keys.get(str(key), ())) > 1:
            defects.append(f'duplicate_identity:{key}')
        for field, bounds in numeric.items():
            if field not in payload or payload[field] is None:
                continue
            value = payload[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not bounds[0] <= value <= bounds[1]:
                defects.append(f'out_of_range:{field}')
        result.append((row['row_no'], 'quarantined' if defects else 'accepted',
                       ','.join(defects) if defects else 'contract checks passed'))
    return result


def _evaluate(db, batch_id):
    batch = db.execute('SELECT * FROM quality_batch WHERE id=?', (batch_id,)).fetchone()
    if not batch:
        raise ValueError('unknown quality batch')
    contract = db.execute('SELECT * FROM quality_contract WHERE id=?', (batch['contract_id'],)).fetchone()
    rows = db.execute('SELECT * FROM quality_item WHERE batch_id=? ORDER BY row_no', (batch_id,)).fetchall()
    decisions = _judge(contract, rows)
    for row_no, status, reason in decisions:
        db.execute('UPDATE quality_item SET status=?,reason=? WHERE batch_id=? AND row_no=?',
                   (status, reason, batch_id, row_no))
    count = sum(status == 'quarantined' for _, status, _ in decisions)
    state = 'quarantined' if count else 'ready'
    db.execute('UPDATE quality_batch SET status=? WHERE id=?', (state, batch_id))
    return {'batch_id': batch_id, 'status': state, 'accepted': len(decisions)-count,
            'quarantined': count, 'decisions': [dict(row_no=n, status=s, reason=r) for n, s, r in decisions]}


def evaluate(db_path, batch_id):
    with closing(connect(db_path)) as db, db:
        return _evaluate(db, batch_id)


def propose_repair(db_path, path):
    item = json.loads(Path(path).read_text(encoding='utf-8'))
    ident = str(item['proposal_id'])
    batch_id = int(item['batch_id'])
    row_no = int(item['row_no'])
    actor = str(item['submitter'])
    patch = item['patch']
    if not ident or not actor or not isinstance(patch, dict) or not patch:
        raise ValueError('invalid quality repair proposal')
    with closing(connect(db_path)) as db, db:
        row = db.execute('SELECT * FROM quality_item WHERE batch_id=? AND row_no=?',
                         (batch_id, row_no)).fetchone()
        if not row or row['status'] != 'quarantined':
            raise ValueError('repair target must be quarantined')
        if row['current_sha'] != item['expected_sha']:
            raise ValueError('repair base hash changed')
        old = db.execute('SELECT * FROM quality_repair WHERE id=?', (ident,)).fetchone()
        if old:
            if (old['batch_id'], old['row_no'], old['expected_sha'], old['patch_json'], old['submitter']) != (
                    batch_id, row_no, item['expected_sha'], _canon(patch), actor):
                raise ValueError('proposal id reused with different terms')
            return {'proposal_id': ident, 'status': old['status'], 'idempotent': True}
        db.execute('INSERT INTO quality_repair(id,batch_id,row_no,expected_sha,patch_json,submitter,status,reason) '
                   'VALUES(?,?,?,?,?,?,?,?)', (ident, batch_id, row_no, item['expected_sha'],
                                               _canon(patch), actor, 'pending', 'awaiting independent reviewer'))
    return {'proposal_id': ident, 'status': 'pending'}


def review_repair(db_path, proposal_id, reviewer, approve, reason):
    if not reviewer or not reason:
        raise ValueError('reviewer and reason required')
    with closing(connect(db_path)) as db, db:
        proposal = db.execute('SELECT * FROM quality_repair WHERE id=?', (proposal_id,)).fetchone()
        if not proposal or proposal['status'] != 'pending':
            raise ValueError('proposal missing or already reviewed')
        if proposal['submitter'] == reviewer:
            raise ValueError('repair requires independent reviewer')
        row = db.execute('SELECT * FROM quality_item WHERE batch_id=? AND row_no=?',
                         (proposal['batch_id'], proposal['row_no'])).fetchone()
        if row['current_sha'] != proposal['expected_sha']:
            raise ValueError('repair base changed before review')
        if approve:
            payload = json.loads(row['current_json'])
            payload.update(json.loads(proposal['patch_json']))
            current = _canon(payload)
            db.execute('UPDATE quality_item SET current_json=?,current_sha=? WHERE batch_id=? AND row_no=?',
                       (current, _sha(current.encode()), proposal['batch_id'], proposal['row_no']))
        db.execute('UPDATE quality_repair SET reviewer=?,status=?,reason=? WHERE id=?',
                   (reviewer, 'approved' if approve else 'rejected', reason, proposal_id))
        decision = _evaluate(db, proposal['batch_id']) if approve else None
    return {'proposal_id': proposal_id, 'status': 'approved' if approve else 'rejected',
            'batch': decision}


def release(db_path, batch_id, output=None):
    with closing(connect(db_path)) as db, db:
        decision = _evaluate(db, batch_id)
        rows = db.execute('SELECT * FROM quality_item WHERE batch_id=? ORDER BY row_no', (batch_id,)).fetchall()
        manifest = [{'source_id': row['source_id'], 'sha': row['current_sha'],
                     'payload': json.loads(row['current_json'])} for row in rows]
        manifest_sha = _sha(_canon(manifest).encode()) if decision['quarantined'] == 0 else None
        status = 'released' if manifest_sha else 'blocked'
        reason = 'all source identities and semantic rules accepted' if manifest_sha else 'quarantined rows remain'
        release_id = db.execute('INSERT INTO quality_release(batch_id,status,accepted_count,quarantined_count,manifest_sha,reason) '
                                'VALUES(?,?,?,?,?,?)', (batch_id, status, decision['accepted'],
                                                       decision['quarantined'], manifest_sha, reason)).lastrowid
        if status == 'released' and output:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            fields = sorted(set().union(*(entry['payload'].keys() for entry in manifest)))
            with target.open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(entry['payload'] for entry in manifest)
    return {'release_id': release_id, 'batch_id': batch_id, 'status': status,
            'accepted': decision['accepted'], 'quarantined': decision['quarantined'],
            'manifest_sha': manifest_sha, 'reason': reason, 'output': str(output) if status == 'released' and output else None}


def quality_report(db_path):
    with closing(connect(db_path)) as db:
        tables = ('quality_contract', 'quality_batch', 'quality_item', 'quality_repair', 'quality_release')
        out = {}
        for table in tables:
            columns = '*' if table not in ('quality_contract', 'quality_batch') else (
                'id,table_name,identity_field,required_json,numeric_json,contract_sha' if table == 'quality_contract'
                else 'id,contract_id,source_sha,status,created_at')
            out[table] = [dict(row) for row in db.execute(f'SELECT {columns} FROM {table} ORDER BY 1')]
        return out
