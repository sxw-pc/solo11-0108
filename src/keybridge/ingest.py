"""Immutable target generations and their import contracts.

A target import never mutates earlier target history.  Every accepted import
publishes a new generation whose parent is the generation that was active when
the import started.  The active pointer and the materialised ``target_row`` view
move to the new generation atomically.

Two import contracts are explicit:

``full_snapshot``
    The file describes the complete target content of every table it names.
    Keys that are absent from a snapshot table are retired in the new
    generation.  Tables that are not part of the import are carried over.

``delta``
    The file expresses explicit changes only.  Keys that are absent keep the
    parent generation's payload.  A delta names the generation it was prepared
    against; if another importer advanced the active generation in the
    meantime, publishing is refused as stale so it cannot overwrite new rows.

The historical default, ``legacy``, keeps the pre-generation append semantics
for callers that have not opted into a contract: absent keys are retained and a
changed payload for an already present key is rejected with
``target_snapshot_conflict``.
"""

import csv
import hashlib
import io
import json
from pathlib import Path

from .store import connect

MODES = ('full_snapshot', 'delta')
LEGACY_MODE = 'legacy'
_FULL_BASE = 0


def read_csv(path):
    raw = Path(path).read_bytes()
    return raw, list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json(payload):
    return json.dumps(payload, sort_keys=True)


def contract_digest(db):
    rows = [tuple(row) for row in db.execute(
        'SELECT name,pk,natural,refs FROM table_contract ORDER BY name')]
    return hashlib.sha256(_json(rows).encode('utf-8')).hexdigest()


def rows_digest(rows):
    ordered = sorted((table, key, payload) for (table, key), payload in rows.items())
    return hashlib.sha256(_json(ordered).encode('utf-8')).hexdigest()


def generation_digest(db, generation_id):
    return rows_digest(generation_rows(db, generation_id))


def active_generation(db):
    return db.execute(
        'SELECT g.* FROM target_active a JOIN target_generation g ON g.id=a.generation_id '
        'WHERE a.slot=1').fetchone()


def active_generation_id(db):
    row = active_generation(db)
    return row['id'] if row else None


def generation_rows(db, generation_id):
    if generation_id is None:
        return {}
    return {(row['table_name'], row['new_key']): row['payload'] for row in db.execute(
        'SELECT table_name,new_key,payload FROM target_generation_row WHERE generation_id=?',
        (generation_id,))}


def materialised_rows(db):
    return {(row['table_name'], row['new_key']): row['payload'] for row in db.execute(
        'SELECT table_name,new_key,payload FROM target_row')}


def seed_rows(db):
    generation_id = active_generation_id(db)
    if generation_id is not None:
        return generation_rows(db, generation_id)
    return materialised_rows(db)


def ensure_generation(db):
    """Create the root generation on first use, adopting any legacy target rows."""
    generation_id = active_generation_id(db)
    if generation_id is not None:
        return generation_id
    return publish_generation(db, None, 'initial', materialised_rows(db))[0]


def is_ancestor(db, ancestor_id, descendant_id):
    if ancestor_id is None:
        return False
    current = descendant_id
    seen = set()
    while current is not None and current not in seen:
        if current == ancestor_id:
            return True
        seen.add(current)
        row = db.execute('SELECT parent_id FROM target_generation WHERE id=?', (current,)).fetchone()
        current = row['parent_id'] if row else None
    return False


def publish_generation(db, parent_id, origin, rows, *, mode=None, table_name=None,
                       source_sha=None, contract_digest_value=None):
    """Insert an immutable generation, make it active and refresh ``target_row``."""
    digest = rows_digest(rows)
    generation_id = db.execute(
        'INSERT INTO target_generation(parent_id,origin,mode,table_name,source_sha,'
        'contract_digest,digest) VALUES (?,?,?,?,?,?,?)',
        (parent_id, origin, mode, table_name, source_sha, contract_digest_value, digest)).lastrowid
    db.executemany(
        'INSERT INTO target_generation_row(generation_id,table_name,new_key,payload) VALUES (?,?,?,?)',
        [(generation_id, table, key, payload) for (table, key), payload in sorted(rows.items())])
    db.execute('DELETE FROM target_row')
    db.executemany('INSERT INTO target_row(table_name,new_key,payload) VALUES (?,?,?)',
                   [(table, key, payload) for (table, key), payload in sorted(rows.items())])
    db.execute('INSERT INTO target_active(slot,generation_id) VALUES (1,?) '
               'ON CONFLICT(slot) DO UPDATE SET generation_id=excluded.generation_id',
               (generation_id,))
    return generation_id, digest


def _parse_target(path):
    raw, rows = read_csv(path)
    if rows:
        fieldnames = list(rows[0])
    else:
        fieldnames = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))).fieldnames or [])
    if 'new_key' not in fieldnames:
        raise ValueError('missing_column')
    result = {}
    for row in rows:
        key = row.get('new_key')
        if key is None or not str(key).strip():
            raise ValueError('missing_key_value')
        key = str(key)
        payload = _json(row)
        if key in result and result[key] != payload:
            raise ValueError('target_snapshot_conflict')
        result[key] = payload
    return _sha(raw), result


def _normalise_files(files):
    if isinstance(files, dict):
        items = list(files.items())
    else:
        items = list(files)
    if not items:
        raise ValueError('empty_target_import')
    tables = [str(item[0]) for item in items]
    if len(tables) != len(set(tables)):
        raise ValueError('duplicate_target_table')
    return [(str(name), path) for name, path in items]


def _reference_closure_violation(db, rows):
    """Return True when a retained child points at a parent missing from ``rows``."""
    contracts = {row['name']: row for row in db.execute('SELECT * FROM table_contract')}
    for (table, _key), payload_text in rows.items():
        contract = contracts.get(table)
        if contract is None:
            continue
        try:
            payload = json.loads(payload_text)
        except (TypeError, ValueError):
            raise ValueError('invalid_target_payload')
        for ref in json.loads(contract['refs']):
            fields = ref['field'] if isinstance(ref['field'], list) else [ref['field']]
            parent_table = ref['table']
            marker = '__ref_' + parent_table + '_new_key'
            if len(fields) == 1:
                value = payload.get(fields[0], payload.get(marker))
                if value is None or not str(value).strip():
                    continue
                parent_key = str(value)
            elif marker in payload:
                parent_key = str(payload[marker])
            else:
                values = [payload.get(field) for field in fields]
                if any(value is None or not str(value).strip() for value in values):
                    continue
                parent_key = '|'.join(str(value) for value in values)
            if (parent_table, parent_key) not in rows:
                return True
    return False


def _existing_import(db, parsed, mode, base_key, contract_sha):
    """Return the generation that already published exactly this import contract."""
    requested = {(table, mode, source_sha, base_key, contract_sha)
                 for table, (source_sha, _rows) in parsed.items()}
    candidates = set()
    for table, (source_sha, _rows) in parsed.items():
        for record in db.execute(
                'SELECT generation_id FROM target_generation_import WHERE table_name=? AND mode=? '
                'AND source_sha=? AND base_generation=? AND contract_digest=?',
                (table, mode, source_sha, base_key, contract_sha)):
            candidates.add(record['generation_id'])
    for generation_id in candidates:
        recorded = {(row['table_name'], row['mode'], row['source_sha'],
                     row['base_generation'], row['contract_digest'])
                    for row in db.execute(
                        'SELECT table_name,mode,source_sha,base_generation,contract_digest '
                        'FROM target_generation_import WHERE generation_id=?', (generation_id,))}
        if recorded == requested:
            return generation_id
    return None


def _import(db, files, mode, base_generation):
    if mode not in MODES + (LEGACY_MODE,):
        raise ValueError('unknown_import_mode')
    items = _normalise_files(files)
    contract_sha = contract_digest(db)
    parsed = {table: _parse_target(path) for table, path in items}

    if mode == 'delta':
        if base_generation is None:
            raise ValueError('base_generation_required')
        base_generation = int(base_generation)
        base_key = base_generation
    else:
        base_generation = None
        base_key = _FULL_BASE

    # Replaying exactly the same import contract is idempotent, even when the
    # active generation has since advanced.
    existing = _existing_import(db, parsed, mode, base_key, contract_sha)
    if existing is not None:
        return existing

    active_id = active_generation_id(db)
    if mode == 'delta':
        if active_id != base_generation:
            raise ValueError('stale_base_generation')
        parent_id = base_generation
    else:
        parent_id = active_id

    rows = dict(seed_rows(db))
    if mode == 'full_snapshot':
        for table, (_source_sha, file_rows) in parsed.items():
            for key in [key for key in rows if key[0] == table]:
                del rows[key]
            for key, payload in file_rows.items():
                rows[(table, key)] = payload
    elif mode == 'delta':
        for table, (_source_sha, file_rows) in parsed.items():
            for key, payload in file_rows.items():
                rows[(table, key)] = payload
    else:
        for table, (_source_sha, file_rows) in parsed.items():
            for key, payload in file_rows.items():
                current = rows.get((table, key))
                if current is not None and current != payload:
                    raise ValueError('target_snapshot_conflict')
                rows[(table, key)] = payload

    if mode in MODES and _reference_closure_violation(db, rows):
        raise ValueError('reference_closure_violation')


    # Every accepted target import, including the legacy append contract,
    # publishes an immutable generation so the materialised view and the active
    # pointer can never diverge.
    generation_id, _digest = publish_generation(
        db, parent_id, 'import', rows, mode=mode,
        table_name=items[0][0] if len(items) == 1 else None,
        source_sha=parsed[items[0][0]][0] if len(items) == 1 else None,
        contract_digest_value=contract_sha)
    for table, (source_sha, _rows) in parsed.items():
        db.execute(
            'INSERT INTO target_generation_import(generation_id,table_name,mode,source_sha,'
            'base_generation,contract_digest) VALUES (?,?,?,?,?,?)',
            (generation_id, table, mode, source_sha, base_key, contract_sha))
        raw = Path(dict(items)[table]).read_bytes()
        db.execute('INSERT OR IGNORE INTO source_file(path,sha,raw) VALUES (?,?,?)',
                   (str(dict(items)[table]), source_sha, raw))
    return generation_id


def import_target(db_path, table, path, mode='legacy', base_generation=None):
    """Import one target table under ``full_snapshot``, ``delta`` or legacy mode."""
    db = connect(db_path)
    try:
        with db:
            return _import(db, [(table, path)], mode, base_generation)
    finally:
        db.close()


def import_target_generation(db_path, files, mode='full_snapshot', base_generation=None):
    """Atomically import several target tables as one generation.

    ``files`` maps table names to CSV paths.  The whole generation is rejected
    unless the resulting rows keep every ``table_contract.refs`` edge closed, so
    a parent may only be retired together with (or after) its children.
    """
    db = connect(db_path)
    try:
        with db:
            return _import(db, files, mode, base_generation)
    finally:
        db.close()


def import_rows(db_path, table, path):
    db = connect(db_path)
    raw, rows = read_csv(path)
    h = _sha(raw)
    count = 0
    with db:
        c = db.execute('SELECT * FROM table_contract WHERE name=?', (table,)).fetchone()
        if not c:
            raise ValueError('unknown_table')
        pk_fields = c['pk'].split('|')
        natural_fields = c['natural'].split('|')
        ref_fields = [f for ref in json.loads(c['refs'])
                      for f in (ref['field'] if isinstance(ref['field'], list) else [ref['field']])]
        required = pk_fields + natural_fields + ref_fields
        if not rows or any(f not in rows[0] for f in required):
            raise ValueError('missing_column')
        old = db.execute('SELECT id FROM source_file WHERE sha=?', (h,)).fetchone()
        if old:
            return 0
        sid = db.execute('INSERT INTO source_file(path,sha,raw) VALUES (?,?,?)',
                         (str(path), h, raw)).lastrowid
        for line_no, row in enumerate(rows, 2):
            if any(row.get(f) is None or not str(row[f]).strip() for f in required):
                raise ValueError('missing_key_value')
            key = '|'.join(row[f] for f in pk_fields)
            natural = '|'.join(row[f].strip().casefold() for f in natural_fields)
            payload = _json(row)
            existing = db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?',
                                  (table, key)).fetchone()
            if existing:
                if existing['payload'] != payload:
                    db.execute('INSERT INTO collision_group(table_name,old_key,reason,source_id) '
                               'VALUES (?,?,?,?)', (table, key, 'old_key_conflict', sid))
                continue
            db.execute('INSERT INTO source_row(table_name,old_key,natural,payload,source_id,line_no) '
                       'VALUES (?,?,?,?,?,?)', (table, key, natural, payload, sid, line_no))
            count += 1
    db.close()
    return count
