"""Post-migration legacy corrections with bitemporal, field-level history.

The initial migration is immutable. Every applied correction advances a
separate legacy head and appends one row to :class:`correction_history`. A
history row records two independent timelines:

* **valid time** -- the business interval ``[valid_from, valid_to)`` in which
  the correction's declared fields are effective. ``None`` means unbounded.
* **record time** -- ``recorded_at`` (when the system received the correction)
  plus a monotonically increasing ``recorded_seq``.

A late-arriving correction may revise valid time retroactively, but it can
never rewrite an observation that was already recorded. Queries therefore
always state which timeline they use:

* :func:`query_valid` / ``query_as_of(..., time_dimension='valid')`` answers
  "which field values are effective on this business date?";
* :func:`query_recorded` answers "which field values did the system know at
  this receipt cutoff?".

Corrections are field-level. A history row only owns the fields the
correction changed; fields it did not declare keep their previous value. This
is what preserves target-only annotations while the legacy source is revised.
Primary keys and natural keys stay immutable.
"""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .store import connect, correction_content_sha
from .rules import digest_rows


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _sha(value):
    return hashlib.sha256(_json(value).encode('utf-8')).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _instant(value):
    """Normalise a record-time cutoff to a comparable ISO string.

    A bare calendar date bounds the *end* of that day, so a correction
    received later on the cutoff day is already known. Full timestamps are
    compared as-is, keeping ``recorded_at`` a genuine instant.
    """
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value)
    if len(text) == 10 and text[4] == '-' and text[7] == '-':
        return text + 'T23:59:59.999999'
    return text


def _initial(db, plan, table, old_key):
    source = db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?', (table, old_key)).fetchone()
    action = next((a for a in json.loads(plan['actions']) if a['table'] == table and a['old'] == old_key), None)
    if not source or not action:
        raise ValueError('unknown_migrated_row')
    return json.loads(source['payload']), action['payload'], action['new']


def _project(db, table, old_key, original, after, new_key):
    """Project a legacy correction onto the migrated target shape.

    ``after`` may be a complete legacy row (the historical contract) or a
    partial row that only names the fields the correction owns. Identity
    fields may never change; a stable field omitted from a partial row keeps
    its existing value. A declared field that is absent from ``after`` is a
    deletion, matching the full-row contract.
    """
    contract = db.execute('SELECT * FROM table_contract WHERE name=?', (table,)).fetchone()
    if not contract or not isinstance(after, dict):
        raise ValueError('invalid_correction_payload')
    stable = contract['pk'].split('|') + contract['natural'].split('|')
    if any(field in after and original.get(field) != after.get(field) for field in stable):
        raise ValueError('identity_fields_changed')
    result = dict(after)
    if '|' in contract['pk']:
        result['__new_key'] = new_key
    else:
        result[contract['pk']] = new_key
    for ref in json.loads(contract['refs']):
        fields = ref['field'] if isinstance(ref['field'], list) else [ref['field']]
        if any(field not in after for field in fields):
            continue
        parent_old = '|'.join(str(after[field]) for field in fields)
        parent = db.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?',
                            (ref['table'], parent_old)).fetchone()
        if not parent:
            raise ValueError('unmapped_reference')
        if len(fields) == 1:
            result[fields[0]] = parent['new_key']
        else:
            result['__ref_' + ref['table'] + '_new_key'] = parent['new_key']
    return result


# ---------------------------------------------------------------------------
# interval helpers (half-open, open-ended endpoints represented by None)
# ---------------------------------------------------------------------------

def _low(value):
    """Sortable representation of a lower bound; ``None`` is negative infinity."""
    return (0, '') if value is None else (1, str(value))


def _high(value):
    """Sortable representation of an upper bound; ``None`` is positive infinity."""
    return (2, '') if value is None else (1, str(value))


def _value(bound):
    return None if bound[0] in (0, 2) else bound[1]


def _contains(interval, point):
    start, end = interval
    return _low(start) <= point < _high(end)


def _overlaps(left, right):
    """True when two half-open intervals share a non-empty span."""
    return _low(left[0]) < _high(right[1]) and _low(right[0]) < _high(left[1])


def _interval_of(value):
    """Accept a mapping/tuple/SQLite row and return ``(valid_from, valid_to)``."""
    if isinstance(value, dict) or hasattr(value, 'keys'):
        start = value['valid_from']
        end = value['valid_to']
    else:
        start, end = value
    if start is not None and end is not None and not str(start) < str(end):
        raise ValueError('invalid_valid_interval')
    return (start, end)


def validate_intervals(intervals):
    """Reject a set of intervals that is not pairwise non-overlapping.

    Adjacent half-open intervals (``a.valid_to == b.valid_from``) are legal;
    sharing a non-empty span is not. This is the consistency check applied to
    the *visible* timeline produced by :func:`history_slices`; stored history
    may contain overlapping revisions because a late correction supersedes the
    earlier revision for the fields it declares.
    """
    bounds = sorted((_interval_of(item) for item in intervals),
                    key=lambda pair: (_low(pair[0]), _high(pair[1])))
    for previous, current in zip(bounds, bounds[1:]):
        if _overlaps(previous, current):
            raise ValueError('overlapping_correction_interval')
    return True


# ---------------------------------------------------------------------------
# append-only history helpers
# ---------------------------------------------------------------------------

def _history_rows(db, table=None, old_key=None):
    query = 'SELECT * FROM correction_history'
    clauses, params = [], []
    if table is not None:
        clauses.append('table_name=?')
        params.append(table)
    if old_key is not None:
        clauses.append('old_key=?')
        params.append(old_key)
    if clauses:
        query += ' WHERE ' + ' AND '.join(clauses)
    query += ' ORDER BY table_name,old_key,recorded_seq,id'
    return db.execute(query, params).fetchall()


def _grouped(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault((row['table_name'], row['old_key']), []).append(row)
    return grouped


def _declared(row):
    try:
        values = json.loads(row['declared_fields'])
    except (TypeError, ValueError):
        values = []
    return list(values) if isinstance(values, list) else []


def _source_payload(row):
    try:
        return json.loads(row['source_payload'])
    except (TypeError, ValueError):
        return {}


def _target_payload(row):
    try:
        return json.loads(row['target_payload'])
    except (TypeError, ValueError):
        return {}


def _base_source(db, table, old_key):
    row = db.execute('SELECT payload FROM source_row WHERE table_name=? AND old_key=?',
                     (table, old_key)).fetchone()
    return json.loads(row['payload']) if row else {}


def _known_by(rows, cutoff):
    """Rows visible on the record timeline at ``cutoff``.

    ``cutoff`` may be an ISO receipt timestamp, a :class:`datetime`, a
    ``recorded_seq`` integer, or ``None`` (everything).
    """
    if cutoff is None:
        return list(rows)
    if isinstance(cutoff, int):
        return [row for row in rows if row['recorded_seq'] <= cutoff]
    cutoff = _instant(cutoff)
    return [row for row in rows if str(row['recorded_at']) <= cutoff]


def _effective_at(rows, point):
    if point is None:
        return list(rows)
    if isinstance(point, datetime):
        point = point.isoformat()
    point = (1, str(point))
    return [row for row in rows if _contains(_interval_of(row), point)]


def _field_winners(rows):
    """For every declared field, the row with the greatest record sequence."""
    winners = {}
    for row in sorted(rows, key=lambda item: (item['recorded_seq'], item['id'])):
        for field in _declared(row):
            winners[field] = row
    return winners


def _visible_fields(base, rows, winners):
    merged = dict(base)
    for field, row in winners.items():
        payload = _source_payload(row)
        if field in payload:
            merged[field] = payload[field]
        else:
            merged.pop(field, None)
    return merged


def _revision_payload(row):
    if row is None:
        return None
    return {'history_id': row['id'], 'revision': row['revision'],
            'recorded_at': row['recorded_at'], 'recorded_seq': row['recorded_seq'],
            'valid_from': row['valid_from'], 'valid_to': row['valid_to'],
            'batch_id': row['batch_id'], 'item_id': row['item_id'],
            'declared_fields': _declared(row),
            'source_payload': _source_payload(row)}

def _resolve(db, table, old_key, valid_at=None, recorded_cutoff=None, use_valid=True):
    """Resolve one legacy row on one or both timelines.

    ``use_valid`` controls whether valid time filters the candidate rows.
    Record time always filters by ``recorded_cutoff`` when supplied.
    """
    rows = _known_by(_history_rows(db, table, old_key), recorded_cutoff)
    if use_valid and valid_at is not None:
        rows = _effective_at(rows, valid_at)
    winners = _field_winners(rows)
    base = _base_source(db, table, old_key)
    winner = max(rows, key=lambda row: (row['recorded_seq'], row['id'])) if rows else None
    return {
        'table': table,
        'old_key': old_key,
        'fields': _visible_fields(base, rows, winners),
        'declared_fields': sorted(winners),
        'field_sources': {field: _revision_payload(row) for field, row in sorted(winners.items())},
        'revision': winner['revision'] if winner else 0,
        'history_id': winner['id'] if winner else None,
        'valid_from': winner['valid_from'] if winner else None,
        'valid_to': winner['valid_to'] if winner else None,
        'recorded_at': winner['recorded_at'] if winner else None,
        'recorded_seq': winner['recorded_seq'] if winner else None,
        'base_fields': base,
        '_rows': rows,
    }


def _public(result):
    return {key: value for key, value in result.items() if not key.startswith('_')}


# ---------------------------------------------------------------------------
# public bitemporal queries
# ---------------------------------------------------------------------------

def query_valid(path, table, old_key, as_of):
    """Valid-time query: field values effective on the business date ``as_of``."""
    db = connect(path)
    try:
        result = _public(_resolve(db, table, old_key, valid_at=as_of, use_valid=True))
        result['business_at'] = as_of
        result['business_cutoff'] = as_of
        return result
    finally:
        db.close()


def query_recorded(path, table, old_key, as_of):
    """Record-time query: field values the system knew at receipt cutoff ``as_of``.

    Valid time is deliberately ignored here: the question is what the system
    had recorded by that moment, not which business interval is current.
    """
    db = connect(path)
    try:
        result = _public(_resolve(db, table, old_key, recorded_cutoff=as_of, use_valid=False))
        result['recorded_by'] = as_of
        result['recorded_cutoff'] = as_of
        return result
    finally:
        db.close()


def query_as_of(path, table=None, old_key=None, valid_at=None, recorded_at=None,
                business_at=None, recorded_by=None, as_of=None, time_dimension=None):
    """Bitemporal query with explicit cutoffs for each timeline.

    ``valid_at`` / ``business_at`` pin the business date. ``recorded_at`` /
    ``recorded_by`` pin the system receipt cutoff. When only ``as_of`` is
    given, ``time_dimension`` selects the timeline (default ``'valid'``).
    When table/old_key are omitted every known legacy row is returned.
    """
    if business_at is not None:
        valid_at = business_at
    if recorded_by is not None:
        recorded_at = recorded_by
    if as_of is not None:
        if time_dimension == 'recorded':
            recorded_at = as_of
        else:
            valid_at = as_of
    db = connect(path)
    try:
        identities = sorted({(row['table_name'], row['old_key']) for row in _history_rows(db)})
        if table is not None and old_key is not None:
            identities = [(table, old_key)]
        elif table is not None:
            identities = [identity for identity in identities if identity[0] == table]
        elif old_key is not None:
            identities = [identity for identity in identities if identity[1] == old_key]
        results = []
        for name, key in identities:
            business = _resolve(db, name, key, valid_at=valid_at, use_valid=True)
            recorded = _resolve(db, name, key, recorded_cutoff=recorded_at, use_valid=False)
            combined = {
                'table': name,
                'old_key': key,
                'business_cutoff': valid_at,
                'recorded_cutoff': recorded_at,
                'business_at': valid_at,
                'recorded_by': recorded_at,
                'business_fields': business['fields'],
                'business_revision': business['revision'],
                'business_history_id': business['history_id'],
                'business_field_sources': business['field_sources'],
                'recorded_fields': recorded['fields'],
                'recorded_revision': recorded['revision'],
                'recorded_history_id': recorded['history_id'],
                'recorded_field_sources': recorded['field_sources'],
                'valid_fields': business['fields'],
                'recorded_fields_by_cutoff': recorded['fields'],
            }
            results.append(combined)
        if table is not None and old_key is not None:
            return results[0]
        return results
    finally:
        db.close()


def query_business(path, table, old_key, business_at):
    """Alias for the valid-time contract."""
    return query_valid(path, table, old_key, business_at)


def query_known(path, table, old_key, recorded_by):
    """Alias for the record-time contract."""
    return query_recorded(path, table, old_key, recorded_by)


# ---------------------------------------------------------------------------
# history views and consistency checks
# ---------------------------------------------------------------------------

def _segment_fields(base, rows, point):
    active = [row for row in rows if _contains(_interval_of(row), point)]
    winners = _field_winners(active)
    return active, winners, _visible_fields(base, active, winners)


def history_slices(path, table, old_key):
    """Non-overlapping half-open slices of the valid timeline for one row.

    Late corrections split earlier intervals; interior holes are reported as
    explicit ``gap`` slices (``revision`` 0, the base migrated fields only).
    """
    db = connect(path)
    try:
        rows = _history_rows(db, table, old_key)
        if not rows:
            return []
        base = _base_source(db, table, old_key)
        lows = [_low(_interval_of(row)[0]) for row in rows]
        highs = [_high(_interval_of(row)[1]) for row in rows]
        union_low, union_high = min(lows), max(highs)
        boundaries = sorted({bound for row in rows for bound in
                             (_low(_interval_of(row)[0]), _high(_interval_of(row)[1]))}
                            | {union_low, union_high})
        raw = []
        for start, end in zip(boundaries, boundaries[1:]):
            active, winners, fields = _segment_fields(base, rows, start)
            owner_rows = sorted(winners.values(), key=lambda row: (row['recorded_seq'], row['id']))
            winner = owner_rows[-1] if owner_rows else None
            signature = (winner['id'] if winner else None,
                         tuple(sorted((field, row['id']) for field, row in winners.items())))
            raw.append((start, end, winner, fields, signature,
                        {field: _source_payload(row) for field, row in winners.items()},
                        {field: row['id'] for field, row in winners.items()}))
        merged = []
        for start, end, winner, fields, signature, correction_fields, field_sources in raw:
            if merged and merged[-1][4] == signature and merged[-1][1] == start:
                merged[-1] = (merged[-1][0], end, winner, fields, signature, correction_fields, field_sources)
            else:
                merged.append((start, end, winner, fields, signature, correction_fields, field_sources))
        result = []
        for start, end, winner, fields, _, correction_fields, winner_field_sources in merged:
            slice_row = {
                'valid_from': _value(start),
                'valid_to': _value(end),
                'revision': winner['revision'] if winner else 0,
                'history_id': winner['id'] if winner else None,
                'recorded_at': winner['recorded_at'] if winner else None,
                'recorded_seq': winner['recorded_seq'] if winner else None,
                'fields': fields,
                'correction_fields': {field: payload[field] for field, payload in correction_fields.items()
                                      if field in payload},
                'field_sources': dict(winner_field_sources),
                'gap': winner is None,
            }
            result.append(slice_row)
        validate_intervals(result)
        return result
    finally:
        db.close()


def correction_history(path, table=None, old_key=None, include_slices=True):
    """Append-only correction history plus derived valid-time slices."""
    db = connect(path)
    try:
        rows = _history_rows(db, table, old_key)
        entries = []
        for row in rows:
            entry = {key: row[key] for key in row.keys()}
            entry['declared_fields'] = _declared(row)
            entry['source_payload'] = _source_payload(row)
            entry['target_payload'] = _target_payload(row)
            entries.append(entry)
        result = {'rows': entries}
        if include_slices:
            identities = sorted({(row['table_name'], row['old_key']) for row in rows})
            result['slices'] = {f'{name}:{key}': history_slices(path, name, key)
                                for name, key in identities}
        return result
    finally:
        db.close()


def validate_history(path, table=None, old_key=None):
    """Validate the append-only ledger and every derived valid timeline.

    Rejections:

    * ``invalid_valid_interval`` -- an interval with ``valid_from >= valid_to``;
    * ``overlapping_correction_interval`` -- two observations from the *same
      record moment* (same ``recorded_seq`` or same revision) claim the same
      field over an overlapping business interval, or the derived visible
      timeline is not pairwise non-overlapping;
    * ``correction_history_tampered`` -- a stored row's content digest does
      not match its payload, i.e. the append-only ledger was rewritten in
      place.

    Corrections from different record moments are revisions: a later one
    legitimately overrides an earlier one, and the earlier row stays stored.
    """
    db = connect(path)
    try:
        rows = _history_rows(db, table, old_key)
        checked = 0
        for row in rows:
            _interval_of(row)
            if 'content_sha' in row.keys() and row['content_sha'] is not None:
                if row['content_sha'] != correction_content_sha(row):
                    raise ValueError('correction_history_tampered')
            checked += 1
        grouped = _grouped(rows)
        for (name, key), group in grouped.items():
            by_field = {}
            for row in group:
                for field in _declared(row):
                    by_field.setdefault(field, []).append(row)
            for field, claims in by_field.items():
                for position, left in enumerate(claims):
                    for right in claims[position + 1:]:
                        same_moment = left['recorded_seq'] == right['recorded_seq']
                        same_revision = left['revision'] == right['revision']
                        if (same_moment or same_revision) and _overlaps(_interval_of(left), _interval_of(right)):
                            raise ValueError('overlapping_correction_interval')
            slices = history_slices(path, name, key)
            validate_intervals(slices)
        return {'ok': True, 'rows': checked, 'identities': len(grouped)}
    finally:
        db.close()


validate_correction_history = validate_history


# ---------------------------------------------------------------------------
# import / prepare / apply
# ---------------------------------------------------------------------------

def _parse_batch(raw):
    payload = json.loads(raw.decode('utf-8'))
    batch_recorded_at = None
    if isinstance(payload, dict):
        rows = payload.get('corrections', payload.get('rows', payload.get('items')))
        batch_recorded_at = payload.get('recorded_at')
        if not isinstance(rows, list):
            raise ValueError('empty_correction_batch')
    else:
        rows = payload
    if not isinstance(rows, list) or not rows:
        raise ValueError('empty_correction_batch')
    return rows, batch_recorded_at


def _item_interval(item):
    start = item.get('valid_from')
    end = item.get('valid_to')
    if start is not None and end is not None and not str(start) < str(end):
        raise ValueError('invalid_valid_interval')
    return start, end


def _item_recorded_at(item, fallback=None):
    value = item.get('recorded_at', fallback)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if not str(value).strip():
        raise ValueError('invalid_recorded_at')
    return str(value)


def validate_correction_intervals(items):
    """Reject a batch that claims overlapping intervals for the same identity.

    A batch is one system receipt moment. Two items for the same legacy row
    that declare overlapping business intervals cannot both be "what we
    learned now"; the ambiguity is rejected as ``overlapping_correction_interval``.
    """
    seen = {}
    for item in items:
        identity = (str(item['table']), str(item['old_key']))
        interval = _item_interval(item)
        for previous in seen.get(identity, []):
            if _overlaps(previous, interval):
                raise ValueError('overlapping_correction_interval')
        seen.setdefault(identity, []).append(interval)
    return True


def _stable_fields(db, table):
    contract = db.execute('SELECT pk,natural FROM table_contract WHERE name=?', (table,)).fetchone()
    if not contract:
        return []
    return contract['pk'].split('|') + contract['natural'].split('|')


def _item_fields(item):
    """Optional explicit list of fields a correction declares.

    When absent, :func:`prepare_corrections` derives the declared fields from
    the difference between the previous source state and the correction's
    ``after`` payload. An explicit list lets a caller send a partial row that
    only owns the named fields; all other fields keep their prior values.
    """
    value = item.get('fields')
    if value is None:
        return None
    if not isinstance(value, list) or not value or any(not isinstance(field, str) or not field for field in value):
        raise ValueError('invalid_correction_fields')
    if len(set(value)) != len(value):
        raise ValueError('invalid_correction_fields')
    return list(value)


def import_corrections(path, plan_id, file):
    raw = Path(file).read_bytes()
    rows, batch_recorded_at = _parse_batch(raw)
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
            validate_correction_intervals(rows)
            seen = set()
            prepared = []
            for row in rows:
                identity = (str(row['table']), str(row['old_key']))
                if identity in seen or not isinstance(row.get('after'), dict) or int(row['expected_revision']) < 0:
                    raise ValueError('invalid_correction_item')
                seen.add(identity)
                original, _, _ = _initial(db, plan, *identity)
                start, end = _item_interval(row)
                recorded_at = _item_recorded_at(row, batch_recorded_at)
                declared = _item_fields(row)
                if declared is not None:
                    stable = _stable_fields(db, identity[0])
                    for field in declared:
                        if field in stable and (field not in row['after'] or row['after'][field] != original.get(field)):
                            raise ValueError('identity_fields_changed')
                prepared.append((identity[0], identity[1], int(row['expected_revision']),
                                 _json(row['after']), start, end, recorded_at,
                                 _json(declared) if declared is not None else None))
            batch = db.execute("INSERT INTO correction_batch(plan_id,source_sha,raw,status) VALUES (?,?,?,'received')",
                               (plan_id, source_sha, raw)).lastrowid
            db.executemany("INSERT INTO correction_item(batch_id,table_name,old_key,expected_revision,after_source,status,valid_from,valid_to,recorded_at,declared_fields) "
                           "VALUES (?,?,?,?,?,'received',?,?,?,?)",
                           [(batch, *item) for item in prepared])
            return batch
    finally:
        db.close()


_MISSING = object()


def _field(value):
    return '<missing>' if value is _MISSING else _json(value)


def _merge(base, target, desired, fields=None):
    """Three-way merge.

    ``fields`` restricts the merge to the fields the correction declares. A
    partial correction therefore leaves every other legacy field and every
    target-only annotation untouched. Declared fields absent from ``desired``
    are deletions, matching the full-row behaviour.
    """
    merged = dict(target)
    conflicts = []
    scope = sorted(set(base) | set(desired)) if fields is None else list(fields)
    for field in scope:
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


def _declared_fields(base, after):
    return sorted(field for field in set(base) | set(after) if base.get(field, _MISSING) != after.get(field, _MISSING))


def _prior_source(db, table, old_key, valid_from, original):
    """The legacy source state the new correction starts from.

    History is replayed in record order on top of the frozen original, and
    only the fields each correction declared are overlaid. This makes partial
    corrections accumulate correctly: a later correction sees every earlier
    field update regardless of whether any single payload was a full row.

    * No business interval: replay every recorded correction (latest wins).
    * Explicit business interval: replay only corrections covering that
      instant, so a late correction sees the historical version it revises.
    """
    prior = dict(original)
    if valid_from is None:
        rows = _history_rows(db, table, old_key)
    else:
        point = (1, str(valid_from))
        rows = [row for row in _history_rows(db, table, old_key)
                if _contains(_interval_of(row), point)]
    for row in rows:
        payload = _source_payload(row)
        for field in _declared(row):
            if field in payload:
                prior[field] = payload[field]
            else:
                prior.pop(field, None)
    return prior


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
                source = _prior_source(db, item['table_name'], item['old_key'],
                                       item['valid_from'], original)
                # The merge base is the complete projection of the current
                # recorded state, not the historical state at valid_from.
                # Corrections are applied sequentially, so whatever the last
                # applied revision put in the target is the baseline this
                # correction is compared against. Using the historical state
                # would falsely conflict when adjacent valid intervals touch.
                current = _prior_source(db, item['table_name'], item['old_key'], None, original)
                base = _project(db, item['table_name'], item['old_key'], original,
                                current, new_key) if head else projected
                target_row = db.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?',
                                        (item['table_name'], new_key)).fetchone()
                target = json.loads(target_row['payload']) if target_row else None
                after = json.loads(item['after_source'])
                explicit = json.loads(item['declared_fields']) if item['declared_fields'] else None
                declared = explicit if explicit is not None else _declared_fields(source, after)
                if explicit is None:
                    # A correction only owns the fields it actually carries.
                    # Keys omitted from ``after`` retain their previous value;
                    # this is what makes field-level retention the default.
                    declared = [field for field in declared if field in after]
                conflicts = []
                desired = merged = None
                if revision != item['expected_revision']:
                    reason = 'legacy_revision_changed'
                elif target is None:
                    reason = 'target_row_missing'
                else:
                    try:
                        _item_interval(dict(item))
                        # Whole legacy state after this correction: the
                        # current recorded state plus exactly the fields this
                        # correction owns. Building the desired full row from
                        # the current state keeps correction_head a complete
                        # snapshot even for late/partial corrections.
                        result_source = dict(current)
                        for field in declared:
                            if field in after:
                                result_source[field] = after[field]
                            else:
                                result_source.pop(field, None)
                        desired = _project(db, item['table_name'], item['old_key'],
                                           original, result_source, new_key)
                        scoped = {field: desired[field] for field in declared if field in desired}
                        merged, conflicts = _merge(base, target, scoped, fields=declared)
                        reason = 'field_conflict' if conflicts else 'mergeable'
                    except ValueError as exc:
                        reason = str(exc)
                status = 'ready' if reason == 'mergeable' else 'conflict'
                db.execute('UPDATE correction_item SET status=?,reason=?,base_sha=?,target_sha=?,desired_target=?,merged_target=?,declared_fields=? WHERE id=?',
                           (status, reason, _sha(base), _sha(target), _json(desired) if status == 'ready' else None,
                            _json(merged) if status == 'ready' else None, _json(declared), item['id']))
                db.executemany('INSERT INTO correction_conflict VALUES (?,?,?,?,?)',
                               [(item['id'], *row) for row in conflicts])
                results.append((item['id'], status, reason))
            db.execute("UPDATE correction_batch SET status='prepared' WHERE id=?", (batch_id,))
            return results
    finally:
        db.close()


def _next_recorded_seq(db):
    return db.execute('SELECT COALESCE(MAX(recorded_seq),0)+1 FROM correction_history').fetchone()[0]


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
                recorded_at = _item_recorded_at(dict(item)) or _now()
                # Without an explicit business interval a correction takes
                # effect at the moment it enters the system. It never
                # retroactively stretches back to -infinity by accident.
                valid_from = item['valid_from'] if item['valid_from'] is not None else recorded_at
                declared = json.loads(item['declared_fields']) if item['declared_fields'] else []
                seq = _next_recorded_seq(db)
                history_id = db.execute(
                    'INSERT INTO correction_history(table_name,old_key,batch_id,item_id,revision,source_payload,declared_fields,'
                    'projected_payload,target_payload,valid_from,valid_to,recorded_at,recorded_seq,source_sha,content_sha) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (item['table_name'], item['old_key'], batch_id, item['id'], item['expected_revision'] + 1,
                     item['after_source'], _json(declared), item['desired_target'], item['merged_target'],
                     valid_from, item['valid_to'], recorded_at, seq,
                     _sha(json.loads(item['after_source'])),
                     correction_content_sha({
                         'table_name': item['table_name'], 'old_key': item['old_key'], 'batch_id': batch_id,
                         'item_id': item['id'], 'revision': item['expected_revision'] + 1,
                         'source_payload': item['after_source'], 'declared_fields': _json(declared),
                         'projected_payload': item['desired_target'], 'target_payload': item['merged_target'],
                         'valid_from': valid_from, 'valid_to': item['valid_to'],
                         'recorded_at': recorded_at, 'recorded_seq': seq,
                         'source_sha': _sha(json.loads(item['after_source']))}))).lastrowid
                db.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                           (item['merged_target'], item['table_name'], new_key))
                db.execute('INSERT INTO correction_head(table_name,old_key,revision,source_payload,projected_payload,target_payload,updated_by,'
                           'valid_from,valid_to,recorded_at,recorded_seq,history_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) '
                           'ON CONFLICT(table_name,old_key) DO UPDATE SET revision=excluded.revision,source_payload=excluded.source_payload,'
                           'projected_payload=excluded.projected_payload,target_payload=excluded.target_payload,updated_by=excluded.updated_by,'
                           'valid_from=excluded.valid_from,valid_to=excluded.valid_to,recorded_at=excluded.recorded_at,'
                           'recorded_seq=excluded.recorded_seq,history_id=excluded.history_id',
                           (item['table_name'], item['old_key'], item['expected_revision'] + 1,
                            item['after_source'], item['desired_target'], item['merged_target'], item['id'],
                            valid_from, item['valid_to'], recorded_at, seq, history_id))
                db.execute("UPDATE correction_item SET status='applied' WHERE id=?", (item['id'],))
                applied += 1
            status = 'applied' if applied == len(items) else 'partially_applied' if applied else 'blocked'
            db.execute('UPDATE correction_batch SET status=? WHERE id=?', (status, batch_id))
            return status
    finally:
        db.close()


def correction_report(path, batch_id, include_history=True):
    db = connect(path)
    try:
        batch = db.execute('SELECT id,plan_id,source_sha,status FROM correction_batch WHERE id=?', (batch_id,)).fetchone()
        if not batch:
            raise ValueError('unknown_batch')
        report = {
            'batch': dict(batch),
            'items': [dict(row) for row in db.execute(
                'SELECT id,table_name,old_key,expected_revision,status,reason,valid_from,valid_to,recorded_at,declared_fields '
                'FROM correction_item WHERE batch_id=? ORDER BY id', (batch_id,))],
            'conflicts': [dict(row) for row in db.execute(
                'SELECT c.* FROM correction_conflict c JOIN correction_item i ON i.id=c.item_id '
                'WHERE i.batch_id=? ORDER BY c.item_id,c.field', (batch_id,))],
        }
        if include_history:
            report['history'] = [dict(row) for row in db.execute(
                'SELECT * FROM correction_history WHERE batch_id=? ORDER BY recorded_seq,id', (batch_id,))]
            identities = sorted({(row[0], row[1]) for row in db.execute(
                'SELECT DISTINCT table_name,old_key FROM correction_history WHERE batch_id=?', (batch_id,))})
            report['slices'] = {f'{table}:{key}': history_slices(path, table, key)
                                for table, key in identities}
        return report
    finally:
        db.close()
