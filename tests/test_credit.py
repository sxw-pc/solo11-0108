"""Customer credit decisions beyond invoice/payment identity matching."""

import json
import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest

from keybridge import settlement
from keybridge.credit import (issue_note, register_contract, review_note, revoke_note)
from keybridge.credit_effect import (apply_credit, audit_credit, credit_report, decide_credit,
                                     fulfill_refund, readjudicate_credit)
from keybridge.credit_store import connect

FIXTURES = Path(__file__).parent / 'fixtures'


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def paid_setup(tmp_path, mode='carry'):
    db = tmp_path / 'credit.sqlite'
    settlement.import_file(db, FIXTURES / 'settlement_ok.json')
    register_contract(db, FIXTURES / f'credit_contract_{mode}.json')
    return db


def reallocate_setup(tmp_path, disposition='carry'):
    """I1 is fully bank-paid, then gets a 30.00 carry/refund credit with no offset."""
    db = tmp_path / f'reallocate-{disposition}.sqlite'
    settlement.import_file(db, FIXTURES / 'credit_settlement_full.json')
    register_contract(db, FIXTURES / f'credit_contract_{disposition}_reallocate.json')
    note = json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8'))
    note.update(note_id='CN1', contract_id=f'credit-{disposition}-reallocate', amount='30.00')
    issue_note(db, write(tmp_path, f'note-{disposition}.json', note))
    approval = json.loads((FIXTURES / 'credit_review_carry.json').read_text(encoding='utf-8'))
    approval.update(approval_id='approve-CN1', note_id='CN1')
    review_note(db, write(tmp_path, f'approval-{disposition}.json', approval))
    return db


def readjudication(tmp_path, name='readjudication.json', **fields):
    evidence = {'readjudication_id': 'RA1', 'note_id': 'CN1',
                'reason': 'late bank reversal reviewed'}
    evidence.update(fields)
    return write(tmp_path, name, evidence)


def snapshot(db, note_id='CN1'):
    report = credit_report(db, note_id)
    return (report['decision'], report['applications'], report['refunds'])


def partial_setup(tmp_path):
    db = tmp_path / 'credit.sqlite'
    source = json.loads((FIXTURES / 'settlement_ok.json').read_text(encoding='utf-8'))
    source['payments'] = source['payments'][:1]
    source['allocations'] = source['allocations'][:2]
    settlement.import_file(db, write(tmp_path, 'partial-settlement.json', source))
    register_contract(db, FIXTURES / 'credit_contract_carry.json')
    return db


def approve_carry(db):
    issue_note(db, FIXTURES / 'credit_note_carry.json')
    review_note(db, FIXTURES / 'credit_review_carry.json')


def test_partial_invoice_offset_and_excess_carry_to_future_invoice(tmp_path):
    print(__import__('keybridge').__file__)
    db = partial_setup(tmp_path)
    approve_carry(db)
    decision = decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    assert (decision['offset_cents'], decision['carry_cents'], decision['refund_cents']) == (2000, 3000, 0)
    old_settlement = settlement.report(db)
    assert {x['invoice_id']:x['outstanding_cents'] for x in old_settlement['invoices']}['I1'] == 2000
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    applied = apply_credit(db, FIXTURES / 'credit_application_20.json')
    assert (applied['applied_cents'], applied['remaining_cents'],
            applied['target_effective_outstanding_cents']) == (2000, 1000, 2000)
    before_expiry = audit_credit(db, 'CN1', '2026-11-13T00:00:00Z')
    assert (before_expiry['status'], before_expiry['available_cents']) == ('available', 1000)
    expired = audit_credit(db, 'CN1', '2026-12-02T00:00:00Z')
    assert (expired['status'], expired['available_cents']) == ('expired', 0)
    with pytest.raises(ValueError, match='expired'):
        apply_credit(db, write(tmp_path, 'late-application.json', {
            'application_id':'CA2','note_id':'CN1','target_invoice_id':'I3',
            'amount':'1.00','applied_at':'2026-12-02T00:00:00Z'}))
    assert len(settlement.report(db)['history']) == 2
    assert {x['invoice_id']:x['outstanding_cents'] for x in settlement.report(db)['invoices']}['I3'] == 4000
    assert len(credit_report(db, 'CN1')['applications']) == 1


def test_paid_invoice_creates_refund_obligation_and_partial_fulfillment(tmp_path):
    db = paid_setup(tmp_path, 'refund')
    issue_note(db, FIXTURES / 'credit_note_refund.json')
    review_note(db, FIXTURES / 'credit_review_refund.json')
    decision = decide_credit(db, 'RN1', '2026-11-07T00:00:00Z')
    assert (decision['offset_cents'], decision['carry_cents'], decision['refund_cents']) == (0, 0, 3000)
    first = fulfill_refund(db, FIXTURES / 'credit_refund_10.json')
    assert first['refund_due_cents'] == 2000
    assert audit_credit(db, 'RN1', '2026-11-13T00:00:00Z')['status'] == 'refund_due'
    second = {'refund_id':'CR2','note_id':'RN1','amount':'20.00','paid_at':'2026-11-14T00:00:00Z',
              'actor':'cashier-a','bank_proof_sha':'bank-refund-proof-002'}
    assert fulfill_refund(db, write(tmp_path, 'second-refund.json', second))['refund_due_cents'] == 0
    assert audit_credit(db, 'RN1', '2026-11-15T00:00:00Z')['status'] == 'refund_fulfilled'
    assert len(settlement.report(db)['history']) == 1
    with pytest.raises(ValueError, match='exceeds'):
        fulfill_refund(db, write(tmp_path, 'over-refund.json', {**second,'refund_id':'CR3'}))


def test_same_paid_invoice_and_amount_follow_contract_refund_or_carry(tmp_path):
    carry_db = paid_setup(tmp_path)
    note = json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8'))
    note.update(note_id='CN30', amount='30.00')
    issue_note(carry_db, write(tmp_path, 'carry-30.json', note))
    approval = json.loads((FIXTURES / 'credit_review_carry.json').read_text(encoding='utf-8'))
    approval.update(approval_id='approve-CN30', note_id='CN30')
    review_note(carry_db, write(tmp_path, 'approve-CN30.json', approval))
    carry = decide_credit(carry_db, 'CN30', '2026-11-07T00:00:00Z')
    refund_db = tmp_path / 'refund-separate.sqlite'
    settlement.import_file(refund_db, FIXTURES / 'settlement_ok.json')
    register_contract(refund_db, FIXTURES / 'credit_contract_refund.json')
    issue_note(refund_db, FIXTURES / 'credit_note_refund.json')
    review_note(refund_db, FIXTURES / 'credit_review_refund.json')
    refund = decide_credit(refund_db, 'RN1', '2026-11-07T00:00:00Z')
    assert (carry['carry_cents'], carry['refund_cents']) == (3000, 0)
    assert (refund['carry_cents'], refund['refund_cents']) == (0, 3000)
    assert carry['settlement_run_id'] == refund['settlement_run_id'] == 1


def test_unused_carry_can_be_revoked_and_corrected_without_changing_old_run(tmp_path):
    db = paid_setup(tmp_path)
    approve_carry(db)
    assert decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')['carry_cents'] == 5000
    revoked = revoke_note(db, FIXTURES / 'credit_revoke_carry.json')
    assert revoked['status'] == 'accepted'
    assert audit_credit(db, 'CN1', '2026-11-14T00:00:00Z')['status'] == 'revoked'
    issue_note(db, FIXTURES / 'credit_corrected_note.json')
    corrected_approval = {'approval_id':'approve-CN2','note_id':'CN2','reviewer':'finance-a',
                          'decision':'approve','proof_sha':'finance-approval-CN2',
                          'approved_at':'2026-11-15T00:00:00Z'}
    review_note(db, write(tmp_path, 'approve-CN2.json', corrected_approval))
    assert decide_credit(db, 'CN2', '2026-11-16T00:00:00Z')['carry_cents'] == 2000
    assert len(settlement.report(db)['history']) == 1
    assert credit_report(db, 'CN2')['note']['supersedes'] == 'CN1'


def test_used_carry_revocation_is_disputed_and_cannot_spawn_corrected_note(tmp_path):
    db = paid_setup(tmp_path)
    approve_carry(db)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    apply_credit(db, FIXTURES / 'credit_application_20.json')
    assert revoke_note(db, FIXTURES / 'credit_revoke_carry.json')['status'] == 'disputed'
    assert audit_credit(db, 'CN1', '2026-11-14T00:00:00Z')['status'] == 'disputed_revocation'
    with pytest.raises(ValueError, match='accepted unused'):
        issue_note(db, FIXTURES / 'credit_corrected_note.json')
    assert len(credit_report(db, 'CN1')['applications']) == 1


def test_late_bank_reversal_disputes_old_credit_without_rewriting_settlement_runs(tmp_path):
    db = paid_setup(tmp_path)
    approve_carry(db)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'settlement_reversal.json')
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    assert audit_credit(db, 'CN1', '2026-11-13T00:00:00Z')['status'] == 'disputed_settlement'
    with pytest.raises(ValueError, match='settlement changed'):
        apply_credit(db, FIXTURES / 'credit_application_20.json')
    assert [x['status'] for x in settlement.report(db)['history']][:2] == ['settled', 'open']
    assert credit_report(db, 'CN1')['decision']['settlement_run_id'] == 1


def test_wrong_party_unapproved_overuse_and_replay_conflicts(tmp_path):
    db = paid_setup(tmp_path)
    note = json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8'))
    note['issuer'] = 'stranger'
    with pytest.raises(ValueError, match='issuer'):
        issue_note(db, write(tmp_path, 'bad-issuer.json', note))
    before_invoice_note = {**json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8')),
                           'note_id':'before-invoice','issued_at':'2026-10-31T00:00:00Z'}
    with pytest.raises(ValueError, match='precede invoice'):
        issue_note(db, write(tmp_path, 'before-invoice-note.json', before_invoice_note))
    issue_note(db, FIXTURES / 'credit_note_carry.json')
    with pytest.raises(ValueError, match='approval'):
        decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    approve_carry(db)
    self_approval = json.loads((FIXTURES / 'credit_review_carry.json').read_text(encoding='utf-8'))
    self_approval.update(approval_id='bad-review', reviewer='support-a')
    # Existing approval is immutable; a different note is used below to prove role separation.
    other_note = {**json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8')),
                  'note_id':'CN-other','amount':'10.00'}
    issue_note(db, write(tmp_path, 'other-note.json', other_note))
    self_approval['note_id'] = 'CN-other'
    with pytest.raises(ValueError, match='independent'):
        review_note(db, write(tmp_path, 'self-approval.json', self_approval))
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    too_much = json.loads((FIXTURES / 'credit_application_20.json').read_text(encoding='utf-8'))
    too_much.update(application_id='too-much', amount='60.00')
    with pytest.raises(ValueError, match='available'):
        apply_credit(db, write(tmp_path, 'too-much.json', too_much))
    before_invoice = {**json.loads((FIXTURES / 'credit_application_20.json').read_text(encoding='utf-8')),
                      'application_id':'early-application','applied_at':'2026-11-09T10:00:00Z'}
    with pytest.raises(ValueError, match='predates future invoice'):
        apply_credit(db, write(tmp_path, 'before-invoice.json', before_invoice))
    wrong_party = {'invoices':[{'event_id':'invoice-other','id':'I4','party':'merchant-b','reference':'future',
                                'currency':'CNY','amount':'20.00','at':'2026-11-10T10:00:00Z'}]}
    settlement.import_file(db, write(tmp_path, 'other-invoice.json', wrong_party))
    mismatch = {**json.loads((FIXTURES / 'credit_application_20.json').read_text(encoding='utf-8')),
                'application_id':'wrong-party','target_invoice_id':'I4'}
    with pytest.raises(ValueError, match='crosses customer'):
        apply_credit(db, write(tmp_path, 'wrong-party.json', mismatch))
    assert apply_credit(db, FIXTURES / 'credit_application_20.json')['remaining_cents'] == 3000
    assert apply_credit(db, FIXTURES / 'credit_application_20.json')['idempotent'] is True
    with pytest.raises(ValueError, match='reused'):
        apply_credit(db, write(tmp_path, 'same-id-different.json', {**too_much,'application_id':'CA1'}))


def test_cli_credit_flow_and_original_module_path(tmp_path):
    print(__import__('keybridge').__file__)
    db = partial_setup(tmp_path)
    project = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(project / 'src'))

    def run(*args):
        process = subprocess.run([sys.executable, '-m', 'keybridge', *map(str, args)],
                                 cwd=project, env=env, capture_output=True, text=True, check=True)
        return json.loads(process.stdout)

    # The carry contract was registered in partial_setup; all following business steps use the public CLI.
    run('credit-note', FIXTURES / 'credit_note_carry.json', '--db', db)
    run('credit-review', FIXTURES / 'credit_review_carry.json', '--db', db)
    assert run('credit-decide', '--db', db, '--note', 'CN1', '--as-of', '2026-11-07T00:00:00Z')['carry_cents'] == 3000
    run('settlement-import', FIXTURES / 'credit_future_invoice.json', '--db', db)
    assert run('credit-apply', FIXTURES / 'credit_application_20.json', '--db', db)['remaining_cents'] == 1000
    assert run('credit-audit', '--db', db, '--note', 'CN1', '--as-of', '2026-11-13T00:00:00Z')['status'] == 'available'
    assert run('credit-report', '--db', db, '--note', 'CN1')['decision']['offset_cents'] == 2000



def test_reallocate_unspent_offsets_new_source_deficit_and_preserves_future_application(tmp_path):
    db = reallocate_setup(tmp_path)
    decision = decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    assert (decision['offset_cents'], decision['carry_cents']) == (0, 3000)
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    assert apply_credit(db, FIXTURES / 'credit_application_20.json')['remaining_cents'] == 1000
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    before = snapshot(db)
    pending = audit_credit(db, 'CN1', '2026-11-16T00:00:00Z')
    assert (pending['status'], pending['available_cents']) == ('readjudication_required', 0)
    with pytest.raises(ValueError, match='readjudication required'):
        apply_credit(db, write(tmp_path, 'unused-10.json', {
            'application_id': 'CA2', 'note_id': 'CN1', 'target_invoice_id': 'I3',
            'amount': '10.00', 'applied_at': '2026-11-16T00:00:00Z'}))
    result = readjudicate_credit(db, readjudication(tmp_path))
    assert (result['policy'], result['status'], result['old_run_id'], result['new_run_id']) == (
        'reallocate_unspent', 'reallocated', 1, 3)
    assert (result['added_offset_cents'], result['total_offset_cents']) == (1000, 1000)
    assert (result['applied_cents'], result['available_cents']) == (2000, 0)
    assert result['total_offset_cents'] + result['applied_cents'] <= 3000
    after = audit_credit(db, 'CN1', '2026-11-16T00:00:00Z')
    assert (after['status'], after['available_cents'], after['readjudication_id']) == ('consumed', 0, 1)
    report = credit_report(db, 'CN1')
    # The old decision, old run, first application and the new record all remain traceable.
    assert report['decision']['settlement_run_id'] == 1
    assert report['decision']['offset_cents'] == 0
    assert report['applications'] == before[1]
    assert len(report['applications']) == 1
    assert report['readjudications'][0]['decision_id'] == report['decision']['id']
    assert report['readjudications'][0]['invoice_outstanding_cents'] == 1000
    assert [x['id'] for x in settlement.report(db)['history']] == [1, 2, 3]
    assert {x['invoice_id']: x['outstanding_cents'] for x in settlement.report(db)['invoices']}['I1'] == 1000
    assert readjudicate_credit(db, readjudication(tmp_path))['idempotent'] is True


def test_reallocate_refund_reduces_unpaid_obligation_but_keeps_paid_refund(tmp_path):
    db = reallocate_setup(tmp_path, 'refund')
    assert decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')['refund_cents'] == 3000
    first = {'refund_id': 'CR1', 'note_id': 'CN1', 'amount': '10.00',
             'paid_at': '2026-11-12T10:00:00Z', 'actor': 'cashier-a',
             'bank_proof_sha': 'bank-refund-proof-001'}
    assert fulfill_refund(db, write(tmp_path, 'refund-10.json', first))['refund_due_cents'] == 2000
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    with pytest.raises(ValueError, match='readjudication required'):
        fulfill_refund(db, write(tmp_path, 'late-refund.json', {**first, 'refund_id': 'CR2',
                                                                'amount': '10.00'}))
    result = readjudicate_credit(db, readjudication(tmp_path))
    # 10 already paid stays paid; only the 20 still owed can be reallocated,
    # and the source invoice only lacks 10, so 10 moves and 10 stays refundable.
    assert (result['added_offset_cents'], result['refunded_cents']) == (1000, 1000)
    assert (result['refund_due_cents'], result['available_cents']) == (1000, 0)
    assert result['total_offset_cents'] + result['refunded_cents'] + result['refund_due_cents'] == 3000
    assert credit_report(db, 'CN1')['refunds'][0]['cents'] == 1000
    assert fulfill_refund(db, write(tmp_path, 'final-refund.json', {
        **first, 'refund_id': 'CR2', 'amount': '10.00'}))['refund_due_cents'] == 0
    with pytest.raises(ValueError, match='exceeds'):
        fulfill_refund(db, write(tmp_path, 'over.json', {**first, 'refund_id': 'CR3'}))
    assert audit_credit(db, 'CN1', '2026-11-17T00:00:00Z')['status'] == 'refund_fulfilled'


def test_freeze_review_keeps_applied_amount_and_blocks_all_further_use(tmp_path):
    # A freeze contract is a separate immutable contract for the same party/currency,
    # so it is exercised in its own ledger.
    frozen = tmp_path / 'freeze.sqlite'
    settlement.import_file(frozen, FIXTURES / 'credit_settlement_full.json')
    register_contract(frozen, FIXTURES / 'credit_contract_carry.json')
    note = json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8'))
    note.update(amount='30.00')
    issue_note(frozen, write(tmp_path, 'freeze-note.json', note))
    review_note(frozen, FIXTURES / 'credit_review_carry.json')
    assert decide_credit(frozen, 'CN1', '2026-11-07T00:00:00Z')['carry_cents'] == 3000
    settlement.import_file(frozen, FIXTURES / 'credit_future_invoice.json')
    assert apply_credit(frozen, FIXTURES / 'credit_application_20.json')['remaining_cents'] == 1000
    settlement.import_file(frozen, FIXTURES / 'credit_reversal_i1_10.json')
    assert audit_credit(frozen, 'CN1', '2026-11-16T00:00:00Z')['status'] == 'disputed_settlement'
    result = readjudicate_credit(frozen, readjudication(tmp_path))
    assert (result['policy'], result['status']) == ('freeze_review', 'disputed_settlement')
    assert (result['added_offset_cents'], result['available_cents']) == (0, 0)
    assert result['applied_cents'] == 2000
    # Existing effects are preserved, but nothing new may be spent or paid.
    late = write(tmp_path, 'freeze-unused.json', {
        'application_id': 'CA2', 'note_id': 'CN1', 'target_invoice_id': 'I3',
        'amount': '10.00', 'applied_at': '2026-11-16T00:00:00Z'})
    with pytest.raises(ValueError, match='disputed'):
        apply_credit(frozen, late)
    report = credit_report(frozen, 'CN1')
    assert [x['cents'] for x in report['applications']] == [2000]
    assert report['readjudications'][0]['status'] == 'disputed_settlement'
    assert report['decision']['settlement_run_id'] == 1
    # A later run that restores the source invoice releases the frozen credit
    # through another append-only readjudication.
    settlement.import_file(frozen, write(tmp_path, 'repair.json', {
        'payments': [{'event_id': 'payment-repair', 'id': 'P10', 'party': 'merchant-a',
                      'reference': 'batch-7', 'currency': 'CNY', 'amount': '10.00',
                      'at': '2026-11-17T00:00:00Z'}],
        'allocations': [{'event_id': 'edge-repair', 'payment_id': 'P10',
                         'invoice_id': 'I1', 'amount': '10.00'}]}))
    restored = readjudicate_credit(frozen, readjudication(
        tmp_path, 'restore.json', readjudication_id='RA2', expected_readjudication_id=1))
    assert (restored['status'], restored['available_cents'], restored['old_run_id']) == (
        'confirmed', 1000, 3)
    assert restored['new_run_id'] == 4
    assert audit_credit(frozen, 'CN1', '2026-11-18T00:00:00Z')['status'] == 'available'
    assert apply_credit(frozen, write(tmp_path, 'freeze-late-ok.json', {
        'application_id': 'CA2', 'note_id': 'CN1', 'target_invoice_id': 'I3',
        'amount': '10.00', 'applied_at': '2026-11-18T00:00:00Z'}))['remaining_cents'] == 0
    assert [x['id'] for x in settlement.report(frozen)['history']] == [1, 2, 3, 4]
    assert len(credit_report(frozen, 'CN1')['readjudications']) == 2


def test_readjudication_rejects_stale_evidence_and_never_partially_adjusts(tmp_path):
    db = reallocate_setup(tmp_path)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    before = snapshot(db)
    artifact = readjudication(tmp_path, expected_readjudication_id=7)
    with pytest.raises(ValueError, match='stale prior record'):
        readjudicate_credit(db, artifact)
    with pytest.raises(ValueError, match='old settlement run'):
        readjudicate_credit(db, readjudication(tmp_path, 'run.json', expected_old_run_id=99))
    with pytest.raises(ValueError, match='contract changed'):
        readjudicate_credit(db, readjudication(tmp_path, 'sha.json', expected_contract_sha='deadbeef'))
    with pytest.raises(ValueError, match='invalid credit readjudication'):
        readjudicate_credit(db, write(tmp_path, 'empty.json', {'readjudication_id': 'RA9', 'note_id': 'CN1'}))
    assert snapshot(db) == before
    assert credit_report(db, 'CN1')['readjudications'] == []
    with closing(connect(db)) as conn:
        assert conn.execute('SELECT count(*) FROM credit_readjudication').fetchone()[0] == 0
    first = readjudicate_credit(db, readjudication(tmp_path))
    assert first['status'] == 'reallocated'
    with pytest.raises(ValueError, match='no new settlement run'):
        readjudicate_credit(db, readjudication(tmp_path, 'duplicate.json', readjudication_id='RA2'))
    with pytest.raises(ValueError, match='reused'):
        readjudicate_credit(db, readjudication(tmp_path, 'changed.json', reason='different reason'))
    assert len(credit_report(db, 'CN1')['readjudications']) == 1


def test_consecutive_runs_accumulate_without_ever_exceeding_the_note_face(tmp_path):
    db = reallocate_setup(tmp_path)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    apply_credit(db, FIXTURES / 'credit_application_20.json')
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    first = readjudicate_credit(db, readjudication(tmp_path))
    assert (first['new_run_id'], first['total_offset_cents'], first['available_cents']) == (3, 1000, 0)
    # A second, larger reversal makes the source invoice unpaid by 25 while
    # only 20 of the note has been used: no unspent balance remains to move.
    settlement.import_file(db, write(tmp_path, 'bigger-reversal.json', {
        'reversals': [{'event_id': 'reversal-i1-25', 'payment_id': 'P9', 'invoice_id': 'I1',
                       'amount': '25.00', 'at': '2026-11-17T12:00:00Z'}]}))
    second = readjudicate_credit(db, readjudication(
        tmp_path, 'second.json', readjudication_id='RA2', expected_readjudication_id=1))
    assert (second['old_run_id'], second['new_run_id']) == (3, 4)
    assert (second['added_offset_cents'], second['total_offset_cents']) == (0, 1000)
    assert second['applied_cents'] + second['total_offset_cents'] + second['available_cents'] == 3000
    assert second['status'] == 'confirmed'
    history = credit_report(db, 'CN1')['readjudications']
    assert [(x['old_run_id'], x['new_run_id'], x['prior_readjudication_id']) for x in history] == [
        (1, 3, None), (3, 4, 1)]
    # A run that adds no settlement change cannot spawn a record.
    with pytest.raises(ValueError, match='no new settlement run'):
        readjudicate_credit(db, readjudication(
            tmp_path, 'same-run.json', readjudication_id='RA3', expected_readjudication_id=2))
    assert len(credit_report(db, 'CN1')['readjudications']) == 2


def test_disputed_source_invoice_blocks_reallocation_and_keeps_last_record(tmp_path):
    db = reallocate_setup(tmp_path)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_future_invoice.json')
    apply_credit(db, FIXTURES / 'credit_application_20.json')
    # A conflicting allocation makes the source invoice disputed in the new run.
    settlement.import_file(db, write(tmp_path, 'disputed.json', {
        'payments': [{'event_id': 'payment-bad', 'id': 'P7', 'party': 'merchant-a',
                      'reference': 'batch-7', 'currency': 'CNY', 'amount': '5.00',
                      'at': '2026-11-18T00:00:00Z'}],
        'allocations': [{'event_id': 'edge-bad', 'payment_id': 'P7',
                         'invoice_id': 'I1', 'amount': '5.00'}]}))
    assert {x['invoice_id']: x['state'] for x in settlement.report(db)['invoices']}['I1'] == 'disputed'
    with pytest.raises(ValueError, match='evidence unavailable'):
        readjudicate_credit(db, readjudication(tmp_path))
    assert credit_report(db, 'CN1')['readjudications'] == []
    assert audit_credit(db, 'CN1', '2026-11-18T00:00:00Z')['available_cents'] == 0


def test_cli_readjudicates_and_reports_contract_policy(tmp_path):
    db = reallocate_setup(tmp_path)
    project = Path(__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(project / 'src'))

    def run(*args):
        process = subprocess.run([sys.executable, '-m', 'keybridge', *map(str, args)],
                                 cwd=project, env=env, capture_output=True, text=True, check=True)
        return json.loads(process.stdout)

    run('credit-decide', '--db', db, '--note', 'CN1', '--as-of', '2026-11-07T00:00:00Z')
    run('settlement-import', FIXTURES / 'credit_future_invoice.json', '--db', db)
    run('credit-apply', FIXTURES / 'credit_application_20.json', '--db', db)
    run('settlement-import', FIXTURES / 'credit_reversal_i1_10.json', '--db', db)
    assert run('credit-audit', '--db', db, '--note', 'CN1', '--as-of', '2026-11-16T00:00:00Z')['status'] == 'readjudication_required'
    result = run('credit-readjudicate', readjudication(tmp_path), '--db', db)
    assert (result['status'], result['added_offset_cents']) == ('reallocated', 1000)
    report = run('credit-report', '--db', db, '--note', 'CN1')
    assert report['contract']['settlement_change_policy'] == 'reallocate_unspent'
    assert report['readjudications'][0]['new_run_id'] == 3


def test_contract_policy_is_immutable_and_old_ledger_migrates(tmp_path):
    db = tmp_path / 'legacy.sqlite'
    settlement.import_file(db, FIXTURES / 'credit_settlement_full.json')
    register_contract(db, FIXTURES / 'credit_contract_carry.json')
    changed = json.loads((FIXTURES / 'credit_contract_carry.json').read_text(encoding='utf-8'))
    changed['settlement_change_policy'] = 'reallocate_unspent'
    with pytest.raises(ValueError, match='reused with changed terms'):
        register_contract(db, write(tmp_path, 'changed-contract.json', changed))
    with closing(connect(db)) as conn:
        policy = conn.execute("SELECT settlement_change_policy FROM credit_contract WHERE id=?",
                              ('credit-carry-a',)).fetchone()[0]
    assert policy == 'freeze_review'
    # A ledger created before the column and readjudication link existed gains
    # the freeze policy by default rather than failing on the next transaction.
    import sqlite3
    old = tmp_path / 'old.sqlite'
    con = sqlite3.connect(old)
    con.executescript('CREATE TABLE credit_contract(id TEXT PRIMARY KEY, party TEXT NOT NULL, '
                      'currency TEXT NOT NULL, disposition TEXT NOT NULL, issuers_json TEXT NOT NULL, '
                      'approvers_json TEXT NOT NULL, refunders_json TEXT NOT NULL, source_sha TEXT NOT NULL, '
                      'raw BLOB NOT NULL, UNIQUE(party,currency));'
                      'CREATE TABLE credit_audit(id INTEGER PRIMARY KEY, note_id TEXT NOT NULL, as_of TEXT NOT NULL, '
                      'status TEXT NOT NULL, available_cents INTEGER NOT NULL, refund_due_cents INTEGER NOT NULL, '
                      'current_run_id INTEGER, reason TEXT NOT NULL);')
    con.commit()
    con.close()
    with closing(connect(old)) as conn:
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(credit_contract)')}
        audit_columns = {row['name'] for row in conn.execute('PRAGMA table_info(credit_audit)')}
    assert {'settlement_change_policy'} <= columns
    assert 'readjudication_id' in audit_columns


def test_two_notes_on_one_invoice_share_the_unpaid_capacity(tmp_path):
    db = tmp_path / 'two-notes.sqlite'
    settlement.import_file(db, FIXTURES / 'credit_settlement_full.json')
    register_contract(db, FIXTURES / 'credit_contract_carry_reallocate.json')
    base = json.loads((FIXTURES / 'credit_note_carry.json').read_text(encoding='utf-8'))
    base.update(contract_id='credit-carry-reallocate', amount='30.00')
    issue_note(db, write(tmp_path, 'first-note.json', base))
    approval = json.loads((FIXTURES / 'credit_review_carry.json').read_text(encoding='utf-8'))
    review_note(db, write(tmp_path, 'first-review.json', {**approval, 'approval_id': 'approve-CN1'}))
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    second = dict(base, note_id='CN2', issued_at='2026-11-08T10:00:00Z', amount='30.00')
    issue_note(db, write(tmp_path, 'second-note.json', second))
    review_note(db, write(tmp_path, 'second-review.json',
                         {**approval, 'approval_id': 'approve-CN2', 'note_id': 'CN2',
                          'approved_at': '2026-11-08T11:00:00Z'}))
    assert decide_credit(db, 'CN2', '2026-11-08T12:00:00Z')['carry_cents'] == 3000
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    first = readjudicate_credit(db, readjudication(tmp_path, 'first-ra.json',
                                                   reason='first note review'))
    assert first['added_offset_cents'] == 1000
    # The two notes share the same 10.00 source-invoice deficit: once CN1 has
    # claimed it, CN2's readjudication may not offset it a second time.
    second = readjudicate_credit(db, readjudication(tmp_path, 'second-ra.json',
                                                    readjudication_id='RA2', note_id='CN2'))
    assert (second['added_offset_cents'], second['total_offset_cents']) == (0, 0)
    assert second['available_cents'] == 3000
    combined = sum(x['total_offset_cents'] for x in
                   credit_report(db, 'CN1')['readjudications'] +
                   credit_report(db, 'CN2')['readjudications'])
    assert combined == 1000
    assert combined + second['applied_cents'] <= 3000


def test_over_face_ledger_inconsistency_rejects_whole_readjudication(tmp_path):
    db = reallocate_setup(tmp_path)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    # A ledger that already accounts for more than the note face (for example
    # through an out-of-band repair) cannot be silently netted by a readjudication.
    with closing(connect(db)) as conn, conn:
        conn.execute('INSERT INTO credit_application VALUES(?,?,?,?,?,?,?)',
                     ('CA-bad', 'CN1', 'I1', 4000, '2026-11-12T10:00:00Z', 'bad', b'bad'))
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    with pytest.raises(ValueError, match='inconsistent'):
        readjudicate_credit(db, readjudication(tmp_path))
    assert credit_report(db, 'CN1')['readjudications'] == []
    assert audit_credit(db, 'CN1', '2026-11-16T00:00:00Z')['available_cents'] == 0


def test_readjudicated_offset_marks_carry_used_for_revocation(tmp_path):
    db = reallocate_setup(tmp_path)
    assert decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')['offset_cents'] == 0
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    readjudicate_credit(db, readjudication(tmp_path))
    revoked = revoke_note(db, write(tmp_path, 'revoke.json', {
        'revocation_id': 'CV1', 'note_id': 'CN1', 'actor': 'finance-a',
        'revoked_at': '2026-11-18T10:00:00Z', 'reason': 'source credit corrected'}))
    assert revoked['status'] == 'disputed'
    assert audit_credit(db, 'CN1', '2026-11-18T11:00:00Z')['status'] == 'disputed_revocation'


def test_bank_repayment_making_prior_offset_excessive_rejects_next_readjudication(tmp_path):
    db = reallocate_setup(tmp_path)
    decide_credit(db, 'CN1', '2026-11-07T00:00:00Z')
    settlement.import_file(db, FIXTURES / 'credit_reversal_i1_10.json')
    first = readjudicate_credit(db, readjudication(tmp_path))
    assert first['total_offset_cents'] == 1000
    # The bank later repays the reversal, so I1 is settled again and the
    # recorded credit offset no longer fits the bank unpaid amount.
    settlement.import_file(db, write(tmp_path, 'repayment.json', {
        'payments': [{'event_id': 'payment-repay', 'id': 'P10', 'party': 'merchant-a',
                      'reference': 'batch-7', 'currency': 'CNY', 'amount': '10.00',
                      'at': '2026-11-17T00:00:00Z'}],
        'allocations': [{'event_id': 'edge-repay', 'payment_id': 'P10',
                         'invoice_id': 'I1', 'amount': '10.00'}]}))
    with pytest.raises(ValueError, match='exceed current outstanding'):
        readjudicate_credit(db, readjudication(
            tmp_path, 'second.json', readjudication_id='RA2', expected_readjudication_id=1))
    report = credit_report(db, 'CN1')
    assert [(x['id'], x['total_offset_cents']) for x in report['readjudications']] == [(1, 1000)]
    current = audit_credit(db, 'CN1', '2026-11-18T00:00:00Z')
    assert (current['status'], current['available_cents'], current['readjudication_id']) == (
        'readjudication_required', 0, None)
    audits = credit_report(db, 'CN1')['audits']
    assert [x['readjudication_id'] for x in audits] == [None]
    assert audits[0]['current_run_id'] == 3
