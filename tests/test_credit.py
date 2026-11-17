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
                                     fulfill_refund)
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

