"""Post-migration legacy corrections with field-level three-way reconciliation.

The initial migration is immutable. A correction advances a separate legacy head and
updates the migrated target only when the observed target still permits a merge.
"""
import hashlib
import json
from pathlib import Path

from .store import connect
from .rules import digest_rows


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _initial(db, plan, table, old_key):
    source = db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?', (table, old_key)).fetchone()
    action = next((a for a in json.loads(plan['actions']) if a['table'] == table and a['old'] == old_key), None)
    if not source or not action:
        raise ValueError('unknown_migrated_row')
    return json.loads(source['payload']), action['payload'], action['new']


def _project(db, table, old_key, original, after, new_key):
    contract = db.execute('SELECT * FROM table_contract WHERE name=?', (table,)).fetchone()
    if not contract or not isinstance(after, dict):
        raise ValueError('invalid_correction_payload')
    stable = contract['pk'].split('|') + contract['natural'].split('|')
    if any(original.get(field) != after.get(field) for field in stable):
        raise ValueError('identity_fields_changed')
    result = dict(after)
    if '|' in contract['pk']:
        result['__new_key'] = new_key
    else:
        result[contract['pk']] = new_key
    for ref in json.loads(contract['refs']):
        fields = ref['field'] if isinstance(ref['field'], list) else [ref['field']]
        try:
            parent_old = '|'.join(str(after[field]) for field in fields)
        except KeyError as exc:
            raise ValueError('missing_reference_field') from exc
        parent = db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',
                            (ref['table'], parent_old)).fetchone()
        if not parent:
            raise ValueError('unmapped_reference')
        if len(fields) == 1:
            result[fields[0]] = parent['new_key']
        else:
            result['__ref_' + ref['table'] + '_new_key'] = parent['new_key']
    return result


def import_corrections(path, plan_id, file):
    raw = Path(file).read_bytes()
    rows = json.loads(raw.decode('utf-8'))
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty_correction_batch')
    db = connect(path)
    try:
        with db:
            plan = db.execute('SELECT * FROM migration_plan WHERE id=?', (plan_id,)).fetchone()
            if not plan or plan['status'] != 'applied':
                raise ValueError('migration_not_applied')
            if digest_rows(db, 'source_row') != plan['source_hash']:
                raise ValueError('migration_source_changed')
            source_sha = hashlib.sha256(raw).hexdigest()
            old = db.execute('SELECT id,plan_id FROM correction_batch WHERE source_sha=?', (source_sha,)).fetchone()
            if old:
                if old['plan_id'] != plan_id:
                    raise ValueError('correction_batch_plan_conflict')
                return old['id']
            seen = set()
            for row in rows:
                identity = (str(row['table']), str(row['old_key']))
                if identity in seen or not isinstance(row.get('after'), dict) or int(row['expected_revision']) < 0:
                    raise ValueError('invalid_correction_item')
                seen.add(identity)
                _initial(db, plan, *identity)
            batch = db.execute("INSERT INTO correction_batch(plan_id,source_sha,raw,status) VALUES (?,?,?,'received')",
                               (plan_id, source_sha, raw)).lastrowid
            db.executemany("INSERT INTO correction_item(batch_id,table_name,old_key,expected_revision,after_source,status) VALUES (?,?,?,?,?,'received')",
                           [(batch, str(r['table']), str(r['old_key']), int(r['expected_revision']), _json(r['after'])) for r in rows])
            return batch
    finally:
        db.close()


_MISSING = object()


def _field(value):
    return '<missing>' if value is _MISSING else _json(value)


def _merge(base, target, desired):
    merged = dict(target)
    conflicts = []
    for field in sorted(set(base) | set(desired)):
        before = base.get(field, _MISSING)
        after = desired.get(field, _MISSING)
        if before == after:
            continue
        current = target.get(field, _MISSING)
        if current != before and current != after:
            conflicts.append((field, _field(before), _field(current), _field(after)))
        elif after is _MISSING:
            merged.pop(field, None)
        else:
            merged[field] = after
    return merged, conflicts


def prepare_corrections(path, batch_id):
    db = connect(path)
    try:
        with db:
            batch = db.execute('SELECT * FROM correction_batch WHERE id=?', (batch_id,)).fetchone()
            if not batch or batch['status'] != 'received':
                raise ValueError('batch_not_received')
            plan = db.execute('SELECT * FROM migration_plan WHERE id=?', (batch['plan_id'],)).fetchone()
            if plan['status'] != 'applied':
                raise ValueError('migration_not_applied')
            if digest_rows(db, 'source_row') != plan['source_hash']:
                raise ValueError('migration_source_changed')
            results = []
            for item in db.execute('SELECT * FROM correction_item WHERE batch_id=? ORDER BY id', (batch_id,)).fetchall():
                original, projected, new_key = _initial(db, plan, item['table_name'], item['old_key'])
                head = db.execute('SELECT * FROM correction_head WHERE table_name=? AND old_key=?',
                                  (item['table_name'], item['old_key'])).fetchone()
                revision = head['revision'] if head else 0
                base = json.loads(head['projected_payload']) if head else projected
                source = json.loads(head['source_payload']) if head else original
                target_row = db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                        (item['table_name'], new_key)).fetchone()
                target = json.loads(target_row['payload']) if target_row else None
                conflicts = []
                if revision != item['expected_revision']:
                    reason = 'legacy_revision_changed'
                elif target is None:
                    reason = 'target_row_missing'
                else:
                    try:
                        desired = _project(db, item['table_name'], item['old_key'], source,
                                           json.loads(item['after_source']), new_key)
                        merged, conflicts = _merge(base, target, desired)
                        reason = 'field_conflict' if conflicts else 'mergeable'
                    except ValueError as exc:
                        reason = str(exc)
                status = 'ready' if reason == 'mergeable' else 'conflict'
                db.execute('UPDATE correction_item SET status=?,reason=?,base_sha=?,target_sha=?,desired_target=?,merged_target=? WHERE id=?',
                           (status, reason, _sha(base), _sha(target), _json(desired) if status == 'ready' else None,
                            _json(merged) if status == 'ready' else None, item['id']))
                db.executemany('INSERT INTO correction_conflict VALUES (?,?,?,?,?)',
                               [(item['id'], *row) for row in conflicts])
                results.append((item['id'], status, reason))
            db.execute("UPDATE correction_batch SET status='prepared' WHERE id=?", (batch_id,))
            return results
    finally:
        db.close()


def apply_corrections(path, batch_id):
    db = connect(path)
    try:
        with db:
            batch = db.execute('SELECT * FROM correction_batch WHERE id=?', (batch_id,)).fetchone()
            if not batch or batch['status'] not in {'prepared', 'applied', 'partially_applied', 'blocked'}:
                raise ValueError('batch_not_prepared')
            if batch['status'] != 'prepared':
                return batch['status']
            plan = db.execute('SELECT * FROM migration_plan WHERE id=?', (batch['plan_id'],)).fetchone()
            if plan['status'] != 'applied':
                raise ValueError('migration_not_applied')
            if digest_rows(db, 'source_row') != plan['source_hash']:
                raise ValueError('migration_source_changed')
            items = db.execute('SELECT * FROM correction_item WHERE batch_id=? ORDER BY id', (batch_id,)).fetchall()
            for item in items:
                if item['status'] != 'ready':
                    continue
                original, projected, new_key = _initial(db, plan, item['table_name'], item['old_key'])
                head = db.execute('SELECT revision,projected_payload FROM correction_head WHERE table_name=? AND old_key=?',
                                  (item['table_name'], item['old_key'])).fetchone()
                revision = head['revision'] if head else 0
                base = json.loads(head['projected_payload']) if head else projected
                target_row = db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                        (item['table_name'], new_key)).fetchone()
                target = json.loads(target_row['payload']) if target_row else None
                if revision != item['expected_revision'] or _sha(base) != item['base_sha'] or _sha(target) != item['target_sha']:
                    raise ValueError('prepared_correction_stale')
            applied = 0
            for item in items:
                if item['status'] != 'ready':
                    continue
                new_key = db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',
                                     (item['table_name'], item['old_key'])).fetchone()['new_key']
                db.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                           (item['merged_target'], item['table_name'], new_key))
                db.execute('INSERT INTO correction_head(table_name,old_key,revision,source_payload,projected_payload,target_payload,updated_by) VALUES (?,?,?,?,?,?,?) '
                           'ON CONFLICT(table_name,old_key) DO UPDATE SET revision=excluded.revision,source_payload=excluded.source_payload,projected_payload=excluded.projected_payload,target_payload=excluded.target_payload,updated_by=excluded.updated_by',
                           (item['table_name'], item['old_key'], item['expected_revision'] + 1,
                            item['after_source'], item['desired_target'], item['merged_target'], item['id']))
                db.execute("UPDATE correction_item SET status='applied' WHERE id=?", (item['id'],))
                applied += 1
            status = 'applied' if applied == len(items) else 'partially_applied' if applied else 'blocked'
            db.execute('UPDATE correction_batch SET status=? WHERE id=?', (status, batch_id))
            return status
    finally:
        db.close()


def correction_report(path, batch_id):
    db = connect(path)
    try:
        batch = db.execute('SELECT id,plan_id,source_sha,status FROM correction_batch WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise ValueError('unknown_batch')
        return {'batch': dict(batch),
                'items': [dict(row) for row in db.execute('SELECT id,table_name,old_key,expected_revision,status,reason FROM correction_item WHERE batch_id=? ORDER BY id', (batch_id,))],
                'conflicts': [dict(row) for row in db.execute('SELECT c.* FROM correction_conflict c JOIN correction_item i ON i.id=c.item_id WHERE i.batch_id=? ORDER BY c.item_id,c.field', (batch_id,))]}
    finally:
        db.close()
