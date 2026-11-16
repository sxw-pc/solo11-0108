import csv
import json
from pathlib import Path

import pytest

from keybridge.contract import compile_contract
from keybridge.corrections import (
    apply_corrections,
    correction_history,
    correction_report,
    history_slices,
    import_corrections,
    prepare_corrections,
    query_as_of,
    query_recorded,
    query_valid,
    validate_history,
    validate_intervals,
)
from keybridge.cutover import audit_evidence, audit_target
from keybridge.ingest import import_rows
from keybridge.rules import apply, plan as make_plan
from keybridge.store import connect


def build(tmp_path, rows):
    db = tmp_path / 'data.db'
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps({'tables': [{'name': 'account', 'pk': 'id', 'natural': 'code'}]}),
                        encoding='utf-8')
    compile_contract(contract, db)
    file = tmp_path / 'account.csv'
    with file.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'code', 'status', 'owner'])
        writer.writerows(rows)
    import_rows(db, 'account', file)
    plan_id, status, _ = make_plan(db)
    assert status == 'ready'
    apply(db, plan_id)
    return db, plan_id


def batch(db, plan_id, tmp_path, name, items, recorded_at=None):
    file = tmp_path / name
    payload = items if recorded_at is None else {'recorded_at': recorded_at, 'corrections': items}
    file.write_text(json.dumps(payload), encoding='utf-8')
    identifier = import_corrections(db, plan_id, file)
    prepare_corrections(db, identifier)
    return identifier, apply_corrections(db, identifier)


def correction(valid_from=None, valid_to=None, after=None, recorded_at=None, expected_revision=0,
               fields=None):
    item = {'table': 'account', 'old_key': 'a1', 'expected_revision': expected_revision,
            'after': after if after is not None else
            {'id': 'a1', 'code': 'A', 'status': 'approved', 'owner': 'ops'}}
    if valid_from is not None:
        item['valid_from'] = valid_from
    if valid_to is not None:
        item['valid_to'] = valid_to
    if recorded_at is not None:
        item['recorded_at'] = recorded_at
    if fields is not None:
        item['fields'] = fields
    return item


def test_business_and_system_queries_answer_h3_fixture_independently(tmp_path):
    """H3: correction received 12 Feb, valid from 5 Feb; both contracts must hold."""
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    _, status = batch(db, plan_id, tmp_path, 'late.json',
                      [correction(valid_from='2026-02-05', recorded_at='2026-02-12')])
    assert status == 'applied'

    business = query_valid(db, 'account', 'a1', '2026-02-08')
    assert business['fields']['status'] == 'approved'
    assert business['business_cutoff'] == '2026-02-08'

    known = query_recorded(db, 'account', 'a1', '2026-02-08')
    assert known['fields']['status'] == 'pending'
    assert known['recorded_cutoff'] == '2026-02-08'

    # On the receipt day the system does know the approved value.
    assert query_recorded(db, 'account', 'a1', '2026-02-12')['fields']['status'] == 'approved'
    # Before valid_from the business value is still pending.
    assert query_valid(db, 'account', 'a1', '2026-02-04')['fields']['status'] == 'pending'

    combined = query_as_of(db, 'account', 'a1', business_at='2026-02-08', recorded_by='2026-02-08')
    assert combined['business_fields']['status'] == 'approved'
    assert combined['recorded_fields']['status'] == 'pending'
    assert combined['business_cutoff'] == '2026-02-08'
    assert combined['recorded_cutoff'] == '2026-02-08'

    # Using the wrong dimension for the other question produces the classic bug.
    assert query_recorded(db, 'account', 'a1', '2026-02-08')['fields']['status'] != \
        query_valid(db, 'account', 'a1', '2026-02-08')['fields']['status']


def test_late_correction_inside_same_business_interval_splits_the_timeline(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'first.json',
          [correction(valid_from='2026-02-01', recorded_at='2026-02-03',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved', 'owner': 'ops'})])
    batch(db, plan_id, tmp_path, 'late.json',
          [correction(valid_from='2026-02-10', recorded_at='2026-02-20',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked', 'owner': 'ops'},
                      expected_revision=1)])

    assert query_valid(db, 'account', 'a1', '2026-02-05')['fields']['status'] == 'approved'
    assert query_valid(db, 'account', 'a1', '2026-02-15')['fields']['status'] == 'revoked'
    # Recorded ordering stays append-only: the first observation cannot be rewritten.
    assert query_recorded(db, 'account', 'a1', '2026-02-05')['fields']['status'] == 'approved'
    assert query_recorded(db, 'account', 'a1', '2026-02-20')['fields']['status'] == 'revoked'

    slices = history_slices(db, 'account', 'a1')
    assert [(item['valid_from'], item['valid_to']) for item in slices] == \
        [('2026-02-01', '2026-02-10'), ('2026-02-10', None)]
    assert [item['revision'] for item in slices] == [1, 2]
    assert validate_intervals(slices)


def test_backdated_correction_revises_valid_time_without_rewriting_record(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'first.json',
          [correction(valid_from='2026-02-01', recorded_at='2026-02-02',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved'})])
    batch(db, plan_id, tmp_path, 'second.json',
          [correction(valid_from='2026-02-01', recorded_at='2026-02-25',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked'}, expected_revision=1)])

    # The retroactive revision wins on valid time...
    assert query_valid(db, 'account', 'a1', '2026-02-01')['fields']['status'] == 'revoked'
    # ...but the system's earlier observation is immutable.
    assert query_recorded(db, 'account', 'a1', '2026-02-02')['fields']['status'] == 'approved'
    history = correction_history(db, 'account', 'a1')
    assert [row['revision'] for row in history['rows']] == [1, 2]
    assert history['rows'][0]['valid_from'] == '2026-02-01'


def test_overlapping_batch_intervals_are_rejected(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    file = tmp_path / 'overlap.json'
    file.write_text(json.dumps([
        correction(valid_from='2026-02-01', valid_to='2026-02-10', recorded_at='2026-02-15'),
        correction(valid_from='2026-02-05', valid_to='2026-02-20', recorded_at='2026-02-15'),
    ]), encoding='utf-8')
    with pytest.raises(ValueError, match='overlapping_correction_interval'):
        import_corrections(db, plan_id, file)
    con = connect(db)
    assert con.execute('SELECT COUNT(*) FROM correction_batch').fetchone()[0] == 0
    assert con.execute('SELECT COUNT(*) FROM correction_history').fetchone()[0] == 0


def test_adjacent_half_open_intervals_are_allowed_and_create_no_overlap(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'day1.json',
          [correction(valid_from='2026-02-01', valid_to='2026-02-10', recorded_at='2026-02-15',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved'})])
    batch(db, plan_id, tmp_path, 'day10.json',
          [correction(valid_from='2026-02-10', valid_to='2026-02-20', recorded_at='2026-02-16',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked'}, expected_revision=1)])
    assert query_valid(db, 'account', 'a1', '2026-02-10')['fields']['status'] == 'revoked'
    assert query_valid(db, 'account', 'a1', '2026-02-09')['fields']['status'] == 'approved'
    assert validate_intervals(history_slices(db, 'account', 'a1'))
    assert validate_history(db)['ok']


def test_gap_between_intervals_keeps_base_values(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'first.json',
          [correction(valid_from='2026-02-01', valid_to='2026-02-05', recorded_at='2026-02-06',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved'})])
    batch(db, plan_id, tmp_path, 'second.json',
          [correction(valid_from='2026-02-10', valid_to='2026-02-15', recorded_at='2026-02-16',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked'}, expected_revision=1)])

    hole = query_valid(db, 'account', 'a1', '2026-02-07')
    assert hole['fields']['status'] == 'pending'
    assert hole['declared_fields'] == []

    slices = history_slices(db, 'account', 'a1')
    gaps = [item for item in slices if item['gap']]
    assert [(item['valid_from'], item['valid_to']) for item in gaps] == \
        [('2026-02-05', '2026-02-10')]
    assert gaps[0]['fields']['status'] == 'pending'
    assert validate_intervals(slices)


def test_field_level_correction_preserves_untouched_legacy_and_target_only_fields(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    con = connect(db)
    key = con.execute("SELECT new_key FROM identity_map WHERE old_key='a1'").fetchone()[0]
    target = json.loads(con.execute('SELECT payload FROM target_row WHERE new_key=?', (key,)).fetchone()[0])
    target['note'] = 'target annotation'
    with con:
        con.execute('UPDATE target_row SET payload=? WHERE new_key=?', (json.dumps(target), key))

    batch(db, plan_id, tmp_path, 'status.json',
          [correction(valid_from='2026-02-01', recorded_at='2026-02-10',
                      after={'id': 'a1', 'status': 'approved'}, fields=['status'])])

    stored = json.loads(con.execute('SELECT payload FROM target_row WHERE new_key=?', (key,)).fetchone()[0])
    assert stored['status'] == 'approved'
    assert stored['owner'] == 'ops'          # untouched legacy field survives
    assert stored['note'] == 'target annotation'  # target-only field survives
    assert stored['code'] == 'A'
    assert correction_history(db, 'account', 'a1')['rows'][0]['declared_fields'] == ['status']
    assert query_valid(db, 'account', 'a1', '2026-02-05')['fields']['owner'] == 'ops'


def test_identity_fields_cannot_change_through_a_correction(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    file = tmp_path / 'identity.json'
    file.write_text(json.dumps([
        correction(valid_from='2026-02-01', recorded_at='2026-02-02',
                   after={'id': 'a1', 'code': 'B', 'status': 'approved'})]), encoding='utf-8')
    identifier = import_corrections(db, plan_id, file)
    prepared = prepare_corrections(db, identifier)
    assert prepared[0][2] == 'identity_fields_changed'
    assert apply_corrections(db, identifier) == 'blocked'


def test_late_correction_in_the_middle_of_an_interval_splits_it_in_three(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'first.json',
          [correction(valid_from='2026-02-01', valid_to='2026-02-20', recorded_at='2026-02-03',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved', 'owner': 'ops'})])
    batch(db, plan_id, tmp_path, 'mid.json',
          [correction(valid_from='2026-02-10', recorded_at='2026-02-25',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked', 'owner': 'ops'},
                      expected_revision=1)])

    slices = history_slices(db, 'account', 'a1')
    assert [(item['valid_from'], item['valid_to'], item['revision']) for item in slices] == [
        ('2026-02-01', '2026-02-10', 1),
        ('2026-02-10', None, 2),
    ]
    # The open-ended revision supersedes the earlier one from its start onward.
    assert query_valid(db, 'account', 'a1', '2026-02-15')['fields']['status'] == 'revoked'
    assert query_valid(db, 'account', 'a1', '2026-02-25')['fields']['status'] == 'revoked'
    assert query_valid(db, 'account', 'a1', '2026-02-05')['fields']['status'] == 'approved'
    assert validate_intervals(slices)


def test_date_only_record_cutoff_includes_corrections_received_that_day(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'late.json',
          [correction(valid_from='2026-02-05', recorded_at='2026-02-12T18:00:00')])
    assert query_recorded(db, 'account', 'a1', '2026-02-12')['fields']['status'] == 'approved'
    assert query_recorded(db, 'account', 'a1', '2026-02-11')['fields']['status'] == 'pending'


def test_audit_can_bind_only_a_recorded_cutoff(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'late.json',
          [correction(valid_from='2026-02-05', recorded_at='2026-02-12')])
    audit_id, status, details = audit_target(db, plan_id, recorded_cutoff='2026-02-12')
    assert status == 'complete' and not details
    evidence = audit_evidence(db, audit_id)
    assert evidence['audit']['recorded_cutoff'] == '2026-02-12'
    assert evidence['audit']['valid_cutoff'] is None
    assert evidence['evidence'][0]['correction_revision'] == 1


def test_partial_field_corrections_overlap_independently_without_interval_conflict(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    file = tmp_path / 'status.json'
    file.write_text(json.dumps([
        correction(valid_from='2026-02-01', valid_to='2026-02-20', recorded_at='2026-02-03',
                   after={'id': 'a1', 'status': 'approved'}, fields=['status'])]), encoding='utf-8')
    first = import_corrections(db, plan_id, file)
    prepare_corrections(db, first)
    apply_corrections(db, first)
    file = tmp_path / 'owner.json'
    file.write_text(json.dumps([
        correction(valid_from='2026-02-05', valid_to='2026-02-10', recorded_at='2026-02-06',
                   after={'id': 'a1', 'owner': 'finance'}, fields=['owner'],
                   expected_revision=1)]), encoding='utf-8')
    second = import_corrections(db, plan_id, file)
    assert prepare_corrections(db, second)[0][1] == 'ready'
    assert apply_corrections(db, second) == 'applied'

    inside = query_valid(db, 'account', 'a1', '2026-02-07')
    assert inside['fields']['status'] == 'approved'
    assert inside['fields']['owner'] == 'finance'
    outside = query_valid(db, 'account', 'a1', '2026-02-12')
    assert outside['fields']['status'] == 'approved'
    assert outside['fields']['owner'] == 'ops'
    assert validate_history(db)['ok']
    con = connect(db)
    stored = json.loads(con.execute('SELECT payload FROM target_row').fetchone()[0])
    assert stored['owner'] == 'finance' and stored['status'] == 'approved'


def test_audit_evidence_binds_each_field_to_its_own_revision(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    done = 0
    for name, spec in [
        ('status.json', dict(valid_from='2026-02-01', recorded_at='2026-02-02',
                             after={'id': 'a1', 'status': 'approved'}, fields=['status'])),
        ('owner.json', dict(valid_from='2026-02-03', recorded_at='2026-02-04',
                            after={'id': 'a1', 'owner': 'finance'}, fields=['owner'])),
    ]:
        file = tmp_path / name
        file.write_text(json.dumps([correction(expected_revision=done, **spec)]), encoding='utf-8')
        identifier = import_corrections(db, plan_id, file)
        prepare_corrections(db, identifier)
        apply_corrections(db, identifier)
        done += 1
    audit_id, status, details = audit_target(db, plan_id, valid_cutoff='2026-02-10',
                                             recorded_cutoff='2026-02-10')
    assert status == 'complete' and not details
    evidence = next(item for item in audit_evidence(db, audit_id)['evidence'] if item['old_key'] == 'a1')
    assert evidence['field_evidence']['status']['revision'] == 1
    assert evidence['field_evidence']['owner']['revision'] == 2
    assert evidence['field_evidence']['code']['value_source'] == 'migration_plan'
    assert evidence['correction_revision'] == 2


def test_legacy_head_without_history_is_backfilled_on_upgrade(tmp_path):
    """A database written by the previous workflow keeps working after upgrade."""
    import sqlite3

    db = tmp_path / 'legacy.db'
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE source_row(id INTEGER PRIMARY KEY,table_name TEXT,old_key TEXT,natural TEXT,
                                payload TEXT,source_id INTEGER,line_no INTEGER);
        CREATE TABLE correction_batch(id INTEGER PRIMARY KEY,plan_id INTEGER,source_sha TEXT UNIQUE NOT NULL,
                                      raw BLOB NOT NULL,status TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE correction_item(id INTEGER PRIMARY KEY,batch_id INTEGER,table_name TEXT NOT NULL,
                                     old_key TEXT NOT NULL,expected_revision INTEGER NOT NULL,
                                     after_source TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,
                                     base_sha TEXT,target_sha TEXT,desired_target TEXT,merged_target TEXT,
                                     UNIQUE(batch_id,table_name,old_key));
        CREATE TABLE correction_head(table_name TEXT,old_key TEXT,revision INTEGER NOT NULL,
                                     source_payload TEXT NOT NULL,projected_payload TEXT NOT NULL,
                                     target_payload TEXT NOT NULL,updated_by INTEGER,
                                     PRIMARY KEY(table_name,old_key));
    """)
    con.execute("INSERT INTO source_row(table_name,old_key,payload) VALUES ('account','a1',?)",
                (json.dumps({'id': 'a1', 'status': 'pending'}),))
    con.execute("INSERT INTO correction_batch(plan_id,source_sha,raw,status,created_at) "
                "VALUES (1,'x','','applied','2026-02-12 10:00:00')")
    con.execute("INSERT INTO correction_item(batch_id,table_name,old_key,expected_revision,after_source,status) "
                "VALUES (1,'account','a1',0,?,'applied')",
                (json.dumps({'id': 'a1', 'status': 'approved'}),))
    con.execute("INSERT INTO correction_head VALUES ('account','a1',1,?,?,?,1)",
                (json.dumps({'id': 'a1', 'status': 'approved'}),
                 json.dumps({'id': 'ACC', 'status': 'approved'}),
                 json.dumps({'id': 'ACC', 'status': 'approved'})))
    con.commit()
    con.close()

    upgraded = connect(db)
    head = upgraded.execute('SELECT history_id,recorded_at FROM correction_head').fetchone()
    assert head['history_id'] is not None and head['recorded_at'] == '2026-02-12 10:00:00'
    history = upgraded.execute('SELECT * FROM correction_history').fetchall()
    assert len(history) == 1
    assert json.loads(history[0]['declared_fields']) == ['status']
    assert validate_history(db)['ok']
    assert query_recorded(db, 'account', 'a1', '2026-02-13')['fields']['status'] == 'approved'


def test_batch_level_recorded_at_applies_to_items_without_their_own(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    file = tmp_path / 'batch.json'
    file.write_text(json.dumps({'recorded_at': '2026-03-01', 'corrections': [
        correction(valid_from='2026-02-01', after={'id': 'a1', 'status': 'approved'})]}), encoding='utf-8')
    identifier = import_corrections(db, plan_id, file)
    prepare_corrections(db, identifier)
    assert apply_corrections(db, identifier) == 'applied'
    assert query_recorded(db, 'account', 'a1', '2026-02-28')['fields']['status'] == 'pending'
    assert query_recorded(db, 'account', 'a1', '2026-03-01')['fields']['status'] == 'approved'


def test_target_audit_binds_both_cutoffs_and_stays_fixed_after_new_corrections(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'late.json',
          [correction(valid_from='2026-02-05', recorded_at='2026-02-12')])

    known_audit, status, details = audit_target(db, plan_id, valid_cutoff='2026-02-08',
                                                recorded_cutoff='2026-02-12')
    assert status == 'complete' and not details
    evidence = audit_evidence(db, known_audit)
    assert evidence['audit']['valid_cutoff'] == '2026-02-08'
    assert evidence['audit']['recorded_cutoff'] == '2026-02-12'
    parent = next(item for item in evidence['evidence'] if item['old_key'] == 'a1')
    assert parent['value_source'] == 'correction_history'
    assert parent['correction_revision'] == 1
    assert parent['valid_from'] == '2026-02-05'
    assert parent['recorded_at'] == '2026-02-12'
    assert parent['field_evidence']['status']['revision'] == 1

    # An audit taken before the receipt cannot know the corrected value.
    early_audit, early_status, early_details = audit_target(db, plan_id, valid_cutoff='2026-02-08',
                                                            recorded_cutoff='2026-02-08')
    assert early_status == 'divergent'
    assert json.loads(early_details[0][3]) == {'status': 'pending'}
    assert audit_evidence(db, early_audit)['evidence'][0]['field_evidence']['status'] == \
        {'value_source': 'migration_plan'}

    batch(db, plan_id, tmp_path, 'newer.json',
          [correction(valid_from='2026-02-20', recorded_at='2026-03-01',
                      after={'id': 'a1', 'code': 'A', 'status': 'closed'}, expected_revision=1)])

    # The newer correction changed the current target...
    assert query_valid(db, 'account', 'a1', '2026-02-21')['fields']['status'] == 'closed'
    # ...but both stored audits keep their historical conclusions and evidence.
    stored = audit_evidence(db, known_audit)
    assert stored['audit']['status'] == 'complete'
    assert json.loads(stored['evidence'][0]['expected_payload'])['status'] == 'approved'
    assert json.loads(stored['evidence'][0]['actual_payload'])['status'] == 'approved'
    stored_early = audit_evidence(db, early_audit)
    assert stored_early['audit']['status'] == 'divergent'
    assert json.loads(stored_early['evidence'][0]['expected_payload'])['status'] == 'pending'


def test_audit_evidence_is_immutable_at_the_storage_layer(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    audit_id, _, _ = audit_target(db, plan_id)
    con = connect(db)
    with pytest.raises(Exception):
        with con:
            con.execute('UPDATE target_audit_evidence SET verdict=? WHERE audit_id=?', ('tampered', audit_id))
    with pytest.raises(Exception):
        with con:
            con.execute('DELETE FROM target_audit_evidence WHERE audit_id=?', (audit_id,))


def test_correction_history_is_append_only_at_the_storage_layer(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    batch(db, plan_id, tmp_path, 'late.json',
          [correction(valid_from='2026-02-05', recorded_at='2026-02-12')])
    con = connect(db)
    with pytest.raises(Exception):
        with con:
            con.execute("UPDATE correction_history SET valid_from='2026-01-01'")
    # Even if the trigger is removed, the stored content digest exposes the rewrite.
    con.execute('DROP TRIGGER correction_history_no_update')
    with con:
        con.execute("UPDATE correction_history SET valid_from='2026-01-01'")
    with pytest.raises(ValueError, match='correction_history_tampered'):
        validate_history(db)


def test_recording_order_is_preserved_by_recorded_sequence(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    # A correction received later but valid earlier must not reorder record time.
    batch(db, plan_id, tmp_path, 'late_received.json',
          [correction(valid_from='2026-03-01', recorded_at='2026-03-05',
                      after={'id': 'a1', 'code': 'A', 'status': 'approved'})])
    batch(db, plan_id, tmp_path, 'backdated.json',
          [correction(valid_from='2026-02-01', recorded_at='2026-03-10',
                      after={'id': 'a1', 'code': 'A', 'status': 'revoked'}, expected_revision=1)])
    rows = correction_history(db, 'account', 'a1')['rows']
    assert [row['recorded_seq'] for row in rows] == [1, 2]
    assert [row['revision'] for row in rows] == [1, 2]
    assert query_recorded(db, 'account', 'a1', '2026-03-06')['fields']['status'] == 'approved'
    assert query_recorded(db, 'account', 'a1', '2026-03-10')['fields']['status'] == 'revoked'
    assert query_valid(db, 'account', 'a1', '2026-02-15')['fields']['status'] == 'revoked'


def test_existing_single_revision_workflow_still_reports_one_batch(tmp_path):
    db, plan_id = build(tmp_path, [['a1', 'A', 'pending', 'ops']])
    file = tmp_path / 'normal.json'
    file.write_text(json.dumps([
        {'table': 'account', 'old_key': 'a1', 'expected_revision': 0,
         'after': {'id': 'a1', 'code': 'A', 'status': 'approved', 'owner': 'ops'}}]), encoding='utf-8')
    identifier = import_corrections(db, plan_id, file)
    assert prepare_corrections(db, identifier)[0][1] == 'ready'
    assert apply_corrections(db, identifier) == 'applied'
    assert apply_corrections(db, identifier) == 'applied'

    con = connect(db)
    head = con.execute('SELECT revision,history_id FROM correction_head').fetchone()
    assert head['revision'] == 1 and head['history_id'] is not None
    report = correction_report(db, identifier)
    assert report['batch']['status'] == 'applied'
    assert len(report['items']) == 1
    assert len(report['history']) == 1
    assert report['items'][0]['declared_fields'] == '["status"]'
    # No interval was declared: the head-based single-revision audit still works.
    assert audit_target(db, plan_id)[1] == 'complete'


def test_intervals_are_returned_half_open_with_optional_bounds():
    assert validate_intervals([
        {'valid_from': '2026-02-01', 'valid_to': '2026-02-10'},
        {'valid_from': '2026-02-10', 'valid_to': None},
    ])
    with pytest.raises(ValueError, match='overlapping_correction_interval'):
        validate_intervals([
            {'valid_from': '2026-02-01', 'valid_to': '2026-02-10'},
            {'valid_from': '2026-02-09', 'valid_to': '2026-02-12'},
        ])
