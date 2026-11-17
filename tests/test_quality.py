"""Pre-migration source quality decision and independent reviewed remediation."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from keybridge.quality import (evaluate, import_batch, propose_repair, quality_report,
                               register_contract, release, review_repair)


class QualityWorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.fixtures = Path(__file__).parent / 'fixtures'
        self.db = self.root / 'quality.db'
        register_contract(self.db, self.fixtures / 'quality_contract.json')

    def _proposal(self, ident, batch_id, row_no, patch, expected_sha=None):
        if expected_sha is None:
            items = quality_report(self.db)['quality_item']
            expected_sha = next(x['current_sha'] for x in items
                                if x['batch_id'] == batch_id and x['row_no'] == row_no)
        path = self.root / f'{ident}.json'
        path.write_text(json.dumps({'proposal_id': ident, 'batch_id': batch_id,
                                    'row_no': row_no, 'expected_sha': expected_sha,
                                    'patch': patch, 'submitter': 'extract-operator'}), encoding='utf-8')
        return path

    def test_good_source_is_released_with_real_csv_and_manifest(self):
        print(__import__('keybridge').__file__)
        batch_id = import_batch(self.db, 'quality-orders', self.fixtures / 'quality_normal.json')['batch_id']
        self.assertEqual('ready', evaluate(self.db, batch_id)['status'])
        output = self.root / 'released.csv'
        result = release(self.db, batch_id, output)
        self.assertEqual('released', result['status'])
        self.assertEqual(64, len(result['manifest_sha']))
        with output.open(encoding='utf-8', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(['A', 'B'], [x['id'] for x in rows])
        self.assertEqual(['20', '30'], [x['amount'] for x in rows])

    def test_duplicate_identity_and_range_quarantine_until_independent_review(self):
        batch_id = import_batch(self.db, 'quality-orders', self.fixtures / 'quality_quarantine.json')['batch_id']
        first = evaluate(self.db, batch_id)
        self.assertEqual(('quarantined', 1, 2), (first['status'], first['accepted'], first['quarantined']))
        blocked = release(self.db, batch_id, self.root / 'must-not-exist.csv')
        self.assertEqual('blocked', blocked['status'])
        self.assertFalse((self.root / 'must-not-exist.csv').exists())
        proposal = self._proposal('fix-b', batch_id, 2, {'id': 'C', 'amount': 50})
        propose_repair(self.db, proposal)
        with self.assertRaisesRegex(ValueError, 'independent'):
            review_repair(self.db, 'fix-b', 'extract-operator', True, 'self approved')
        approved = review_repair(self.db, 'fix-b', 'quality-lead', True, 'checked source invoice')
        self.assertEqual(('ready', 0), (approved['batch']['status'], approved['batch']['quarantined']))
        output = self.root / 'repaired.csv'
        result = release(self.db, batch_id, output)
        self.assertEqual('released', result['status'])
        self.assertEqual(['A', 'C', 'B'], [x['id'] for x in csv.DictReader(output.open(encoding='utf-8'))])
        report = quality_report(self.db)
        self.assertEqual(['blocked', 'released'], [x['status'] for x in report['quality_release']])
        self.assertEqual('B', json.loads(report['quality_item'][1]['original_json'])['id'])
        self.assertEqual('C', json.loads(report['quality_item'][1]['current_json'])['id'])

    def test_repair_base_hash_and_rejected_proposal_do_not_change_rows(self):
        batch_id = import_batch(self.db, 'quality-orders', self.fixtures / 'quality_quarantine.json')['batch_id']
        evaluate(self.db, batch_id)
        bad = self._proposal('wrong-base', batch_id, 2, {'amount': 50}, 'deadbeef')
        with self.assertRaisesRegex(ValueError, 'base hash'):
            propose_repair(self.db, bad)
        proposal = self._proposal('reject-b', batch_id, 2, {'id': 'C', 'amount': 50})
        propose_repair(self.db, proposal)
        review_repair(self.db, 'reject-b', 'quality-lead', False, 'invoice does not support correction')
        self.assertEqual('quarantined', evaluate(self.db, batch_id)['status'])
        with self.assertRaisesRegex(ValueError, 'already reviewed'):
            review_repair(self.db, 'reject-b', 'another-reviewer', True, 'late change')

    def test_source_identity_is_immutable_and_batch_replay_idempotent(self):
        path = self.fixtures / 'quality_normal.json'
        first = import_batch(self.db, 'quality-orders', path)
        self.assertEqual(first['batch_id'], import_batch(self.db, 'quality-orders', path)['batch_id'])
        duplicate = self.root / 'duplicate.json'
        duplicate.write_text(json.dumps([{'source_id': 'same', 'payload': {'id': 'A'}},
                                         {'source_id': 'same', 'payload': {'id': 'B'}}]), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'duplicate source_id'):
            import_batch(self.db, 'quality-orders', duplicate)
        self.assertEqual(1, len(quality_report(self.db)['quality_batch']))


if __name__ == '__main__':
    unittest.main()
