import json
import csv
from pathlib import Path

import pytest

from keybridge.__main__ import demo
from keybridge.corrections import import_corrections, prepare_corrections, apply_corrections, correction_report
from keybridge.cutover import audit_target, configure_clients, observe_lookup, retirement_decision, _hash
from keybridge.store import connect
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows
from keybridge.rules import plan as make_plan, apply


FIXTURES = Path(__file__).parent / 'fixtures'


def seeded(tmp_path):
    demo(tmp_path)
    db = tmp_path / 'data.db'
    con = connect(db)
    plan = con.execute('SELECT id FROM migration_plan ORDER BY id DESC LIMIT 1').fetchone()[0]
    return db, plan


def target(db, table):
    con = connect(db)
    row = con.execute('SELECT new_key,payload FROM target_row WHERE table_name=?', (table,)).fetchone()
    return row['new_key'], json.loads(row['payload'])


def update_target(db, table, changes):
    key, payload = target(db, table)
    payload.update(changes)
    con = connect(db)
    with con:
        con.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                    (json.dumps(payload), table, key))


def test_disjoint_target_change_is_preserved_and_becomes_auditable(tmp_path):
    db, plan = seeded(tmp_path)
    update_target(db, 'parent', {'note': 'target team annotation'})
    assert audit_target(db, plan)[1] == 'divergent'
    batch = import_corrections(db, plan, FIXTURES / 'correction_normal.json')
    assert all(item[1] == 'ready' for item in prepare_corrections(db, batch))
    assert apply_corrections(db, batch) == 'applied'
    assert apply_corrections(db, batch) == 'applied'
    _, parent = target(db, 'parent')
    _, child = target(db, 'child')
    assert parent['label'] == 'reviewed' and parent['note'] == 'target team annotation'
    assert child['status'] == 'verified' and child['parent_id'] == parent['id']
    assert audit_target(db, plan)[1] == 'complete'
    con = connect(db)
    assert [r['revision'] for r in con.execute('SELECT revision FROM correction_head ORDER BY table_name')] == [1, 1]


def test_competing_field_is_quarantined_while_independent_row_applies(tmp_path):
    db, plan = seeded(tmp_path)
    update_target(db, 'parent', {'label': 'target team chose another value'})
    batch = import_corrections(db, plan, FIXTURES / 'correction_conflict.json')
    prepared = prepare_corrections(db, batch)
    assert sorted(item[1] for item in prepared) == ['conflict', 'ready']
    assert apply_corrections(db, batch) == 'partially_applied'
    _, parent = target(db, 'parent')
    _, child = target(db, 'child')
    assert parent['label'] == 'target team chose another value'
    assert child['status'] == 'verified'
    report = correction_report(db, batch)
    assert report['batch']['status'] == 'partially_applied'
    assert report['conflicts'][0]['field'] == 'label'
    assert audit_target(db, plan)[1] == 'divergent'


def test_prepared_batch_rechecks_target_and_keeps_all_rows_on_stale_input(tmp_path):
    db, plan = seeded(tmp_path)
    batch = import_corrections(db, plan, FIXTURES / 'correction_normal.json')
    prepare_corrections(db, batch)
    update_target(db, 'child', {'note': 'changed after preparation'})
    with pytest.raises(ValueError, match='prepared_correction_stale'):
        apply_corrections(db, batch)
    assert 'label' not in target(db, 'parent')[1]
    assert correction_report(db, batch)['batch']['status'] == 'prepared'


def test_unmapped_foreign_key_is_a_business_conflict(tmp_path):
    db, plan = seeded(tmp_path)
    batch = import_corrections(db, plan, FIXTURES / 'correction_bad_reference.json')
    assert prepare_corrections(db, batch)[0][2] == 'unmapped_reference'
    assert apply_corrections(db, batch) == 'blocked'
    assert 'status' not in target(db, 'child')[1]


def test_second_legacy_revision_preserves_target_only_field(tmp_path):
    db, plan = seeded(tmp_path)
    first = import_corrections(db, plan, FIXTURES / 'correction_normal.json')
    prepare_corrections(db, first)
    apply_corrections(db, first)
    update_target(db, 'parent', {'note': 'new target annotation'})
    file = tmp_path / 'second.json'
    file.write_text(json.dumps([{'table': 'parent', 'old_key': 'p1', 'expected_revision': 1,
                                 'after': {'id': 'p1', 'code': 'A', 'label': 'released'}}]), encoding='utf-8')
    second = import_corrections(db, plan, file)
    assert prepare_corrections(db, second)[0][1] == 'ready'
    assert apply_corrections(db, second) == 'applied'
    assert target(db, 'parent')[1]['note'] == 'new target annotation'
    assert target(db, 'parent')[1]['label'] == 'released'


def test_legacy_reference_switch_rewrites_to_new_parent_key(tmp_path):
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps({'tables': [
        {'name': 'parent', 'pk': 'id', 'natural': 'code'},
        {'name': 'child', 'pk': 'id', 'natural': 'code', 'refs': [{'field': 'parent_id', 'table': 'parent'}]}
    ]}), encoding='utf-8')
    db = tmp_path / 'data.db'
    compile_contract(contract, db)
    for table, columns, rows in [('parent', ['id', 'code'], [['p1', 'A'], ['p2', 'B']]),
                                 ('child', ['id', 'code', 'parent_id'], [['c1', 'C', 'p1']])]:
        file = tmp_path / f'{table}.csv'
        with file.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            writer.writerows(rows)
        import_rows(db, table, file)
    plan, status, _ = make_plan(db)
    assert status == 'ready'
    apply(db, plan)
    batch = import_corrections(db, plan, FIXTURES / 'correction_reference_switch.json')
    assert prepare_corrections(db, batch)[0][1] == 'ready'
    assert apply_corrections(db, batch) == 'applied'
    con = connect(db)
    p2 = con.execute("SELECT new_key FROM identity_map WHERE table_name='parent' AND old_key='p2'").fetchone()[0]
    assert target(db, 'child')[1]['parent_id'] == p2
    assert audit_target(db, plan)[1] == 'complete'


def test_correction_requires_fresh_client_observation_before_key_retirement(tmp_path):
    db, plan = seeded(tmp_path)
    clients = tmp_path / 'clients.json'
    clients.write_text(json.dumps([{'id': 'catalog', 'critical': True, 'table': 'parent'}]), encoding='utf-8')
    configure_clients(db, clients)
    con = connect(db)
    mapping = con.execute("SELECT old_key,new_key FROM identity_map WHERE table_name='parent'").fetchone()
    before = target(db, 'parent')[1]
    observe_lookup(db, 'catalog', plan, 1, mapping['old_key'], mapping['new_key'], _hash(before))
    old_audit = audit_target(db, plan)[0]
    assert retirement_decision(db, old_audit)[1] == 'retire_old_keys'
    batch = import_corrections(db, plan, FIXTURES / 'correction_normal.json')
    prepare_corrections(db, batch)
    apply_corrections(db, batch)
    assert retirement_decision(db, old_audit)[1] == 'retain_aliases'
    new_audit, status, _ = audit_target(db, plan)
    assert status == 'complete'
    assert retirement_decision(db, new_audit)[1] == 'retain_aliases'
    after = target(db, 'parent')[1]
    observe_lookup(db, 'catalog', plan, 2, mapping['old_key'], mapping['new_key'], _hash(after))
    assert retirement_decision(db, new_audit)[1] == 'retire_old_keys'


# --- irreversible erasure interacts with corrections and retirement ---------
from keybridge.rules import erase


def _csv(tmp, name, header, rows):
    p = tmp / name
    with p.open('w', newline='', encoding='utf-8') as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)
    return p


def erasure_db(tmp_path):
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps({'tables': [
        {'name': 'parent', 'pk': 'id', 'natural': 'code', 'sensitive': ['email']},
        {'name': 'child', 'pk': 'id', 'natural': 'code', 'refs': [{'field': 'parent_id', 'table': 'parent'}]}
    ]}), encoding='utf-8')
    db = tmp_path / 'erase.db'
    compile_contract(contract, db)
    import_rows(db, 'parent', _csv(tmp_path, 'parents.csv', ['id', 'code', 'email', 'region'],
                                   [['p1', 'A', 'p1@example.test', 'north'], ['p2', 'B', 'p2@example.test', 'south']]))
    import_rows(db, 'child', _csv(tmp_path, 'children.csv', ['id', 'code', 'parent_id', 'phone'],
                                  [['c1', 'CA', 'p1', '111'], ['c2', 'CB', 'p1', '222'], ['c3', 'CC', 'p2', '333']]))
    pid, status, _ = make_plan(db)
    assert status == 'ready'
    apply(db, pid)
    return db, pid


def _target(db, table, old_key):
    con = connect(db)
    new_key = con.execute('SELECT new_key FROM identity_map WHERE table_name=? AND old_key=?', (table, old_key)).fetchone()[0]
    row = con.execute('SELECT payload FROM target_row WHERE table_name=? AND new_key=?', (table, new_key)).fetchone()
    return new_key, json.loads(row['payload']) if row else None


def _update_target(db, table, old_key, changes):
    new_key, payload = _target(db, table, old_key)
    payload.update(changes)
    con = connect(db)
    with con:
        con.execute('UPDATE target_row SET payload=? WHERE table_name=? AND new_key=?',
                    (json.dumps(payload, sort_keys=True), table, new_key))


def _assert_value_gone(db, value):
    con = connect(db)
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        for row in con.execute('SELECT * FROM ' + table):
            assert value not in str(tuple(row)), table


def _correction(tmp_path, name, rows):
    file = tmp_path / name
    file.write_text(json.dumps(rows), encoding='utf-8')
    return file


def test_erasure_stales_prepared_batch_and_scrubs_ledger(tmp_path):
    db, pid = erasure_db(tmp_path)
    batch = import_corrections(db, pid, _correction(tmp_path, 'corr.json', [
        {'table': 'parent', 'old_key': 'p1', 'expected_revision': 0,
         'after': {'id': 'p1', 'code': 'A', 'email': 'p1@example.test', 'region': 'north', 'label': 'L'}}]))
    assert prepare_corrections(db, batch)[0][1] == 'ready'
    erase(db, 'parent', 'p1', 'field_only', ['email'], 'req-stale-batch')
    con = connect(db)
    assert con.execute('SELECT status FROM correction_batch WHERE id=?', (batch,)).fetchone()[0] == 'stale'
    with pytest.raises(ValueError, match='batch_not_received'):
        prepare_corrections(db, batch)
    with pytest.raises(ValueError, match='batch_not_prepared'):
        apply_corrections(db, batch)
    item = con.execute('SELECT after_source,desired_target,merged_target FROM correction_item WHERE batch_id=?', (batch,)).fetchone()
    for column in item:
        assert 'email' not in json.loads(column)
    raw = con.execute('SELECT raw FROM correction_batch WHERE id=?', (batch,)).fetchone()[0]
    assert b'p1@example.test' not in bytes(raw)
    _assert_value_gone(db, 'p1@example.test')


def test_erasure_scrubs_head_and_removes_conflicts(tmp_path):
    db, pid = erasure_db(tmp_path)
    first = import_corrections(db, pid, _correction(tmp_path, 'first.json', [
        {'table': 'parent', 'old_key': 'p1', 'expected_revision': 0,
         'after': {'id': 'p1', 'code': 'A', 'email': 'p1@example.test', 'region': 'north', 'label': 'L'}}]))
    prepare_corrections(db, first)
    assert apply_corrections(db, first) == 'applied'
    _update_target(db, 'parent', 'p1', {'email': 'other@x.test'})
    second = import_corrections(db, pid, _correction(tmp_path, 'second.json', [
        {'table': 'parent', 'old_key': 'p1', 'expected_revision': 1,
         'after': {'id': 'p1', 'code': 'A', 'email': 'new@y.test', 'region': 'north', 'label': 'L2'}}]))
    assert prepare_corrections(db, second)[0][1:] == ('conflict', 'field_conflict')
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM correction_conflict WHERE field='email'").fetchone()[0] == 1
    erase(db, 'parent', 'p1', 'field_only', ['email'], 'req-conflict-1')
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM correction_conflict').fetchone()[0] == 0
    head = con.execute("SELECT revision,source_payload,projected_payload,target_payload FROM correction_head WHERE table_name='parent' AND old_key='p1'").fetchone()
    assert head['revision'] == 1
    for column in ('source_payload', 'projected_payload', 'target_payload'):
        assert 'email' not in json.loads(head[column])
    assert con.execute('SELECT status FROM correction_batch WHERE id=?', (first,)).fetchone()[0] == 'applied'
    assert con.execute('SELECT status FROM correction_batch WHERE id=?', (second,)).fetchone()[0] == 'stale'
    _assert_value_gone(db, 'p1@example.test')
    _assert_value_gone(db, 'other@x.test')
    _assert_value_gone(db, 'new@y.test')


def test_correction_reintroducing_erased_field_is_blocked(tmp_path):
    db, pid = erasure_db(tmp_path)
    erase(db, 'parent', 'p1', 'field_only', ['email'], 'req-cblock-1')
    # the sealed plan rejects any new correction batch
    with pytest.raises(ValueError, match='migration_not_applied'):
        import_corrections(db, pid, _correction(tmp_path, 'sealed.json', [
            {'table': 'parent', 'old_key': 'p2', 'expected_revision': 0,
             'after': {'id': 'p2', 'code': 'B', 'email': 'p2@example.test'}}]))
    # rebuilding and applying a fresh plan does not bypass the erasure guard
    pid2, status, _ = make_plan(db)
    assert status == 'ready'
    apply(db, pid2)
    with pytest.raises(ValueError, match='erased_field_reimport'):
        import_corrections(db, pid2, _correction(tmp_path, 'bad.json', [
            {'table': 'parent', 'old_key': 'p1', 'expected_revision': 0,
             'after': {'id': 'p1', 'code': 'A', 'email': 'x@y.test'}}]))
    assert connect(db).execute('SELECT COUNT(*) FROM correction_batch').fetchone()[0] == 0
    # a correction that does not re-introduce the erased field still applies
    batch = import_corrections(db, pid2, _correction(tmp_path, 'ok.json', [
        {'table': 'parent', 'old_key': 'p1', 'expected_revision': 0,
         'after': {'id': 'p1', 'code': 'A', 'region': 'north', 'label': 'L'}}]))
    assert prepare_corrections(db, batch)[0][1] == 'ready'
    assert apply_corrections(db, batch) == 'applied'
    _, payload = _target(db, 'parent', 'p1')
    assert payload['label'] == 'L' and 'email' not in payload


def test_correction_for_erased_entity_is_blocked(tmp_path):
    db, pid = erasure_db(tmp_path)
    erase(db, 'parent', 'p1', 'entity_closure', ['email'], 'req-cblock-2')
    pid2, status, _ = make_plan(db)
    assert status == 'ready'
    apply(db, pid2)
    with pytest.raises(ValueError, match='erased_entity_reimport'):
        import_corrections(db, pid2, _correction(tmp_path, 'bad.json', [
            {'table': 'parent', 'old_key': 'p1', 'expected_revision': 0, 'after': {'id': 'p1', 'code': 'A'}}]))
    with pytest.raises(ValueError, match='erased_entity_reimport'):
        import_corrections(db, pid2, _correction(tmp_path, 'child.json', [
            {'table': 'child', 'old_key': 'c1', 'expected_revision': 0,
             'after': {'id': 'c1', 'code': 'CA', 'parent_id': 'p2'}}]))
    assert connect(db).execute('SELECT COUNT(*) FROM correction_batch').fetchone()[0] == 0


def test_erasure_invalidates_prior_retirement_gate(tmp_path):
    db, pid = erasure_db(tmp_path)
    clients = tmp_path / 'clients.json'
    clients.write_text(json.dumps([{'id': 'catalog', 'critical': True, 'table': 'parent'}]), encoding='utf-8')
    configure_clients(db, clients)
    for sequence, old_key in enumerate(('p1', 'p2'), 1):
        new_key, payload = _target(db, 'parent', old_key)
        observe_lookup(db, 'catalog', pid, sequence, old_key, new_key, _hash(payload))
    audit, status, _ = audit_target(db, pid)
    assert status == 'complete'
    gate, decision, _ = retirement_decision(db, audit)
    assert decision == 'retire_old_keys'
    erase(db, 'parent', 'p1', 'field_only', ['email'], 'req-retire-1')
    con = connect(db)
    assert con.execute('SELECT status FROM retirement_gate WHERE id=?', (gate,)).fetchone()[0] == 'stale'
    # the old audit conclusion can no longer retire keys, and neither can a new one
    assert retirement_decision(db, audit)[1] == 'retain_aliases'
    new_audit, new_status, _ = audit_target(db, pid)
    assert new_status == 'sealed'
    assert retirement_decision(db, new_audit)[1] == 'retain_aliases'
