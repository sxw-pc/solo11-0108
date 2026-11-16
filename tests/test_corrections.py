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
