import json
import tempfile
import unittest
from pathlib import Path

from keybridge.archive import (archive_report, audit_archive, import_candidates,
                               import_replica, plan_archive, register_contract)


class ArchiveWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'archive.sqlite'
        self.fixtures = Path(__file__).parent / 'fixtures'
        register_contract(self.db, self.fixtures / 'archive_contract.json')
        import_candidates(self.db, self.fixtures / 'archive_candidates.json')

    def _proof(self, ident, candidate, domain, digest):
        path = self.root / (ident + '.json')
        path.write_text(json.dumps({'proof_id': ident, 'plan_id': 1,
            'candidate': candidate, 'domain': domain, 'digest': digest}, sort_keys=True), encoding='utf-8')
        return path

    def test_hold_cohort_and_budget_change_retention_choice(self):
        print(__import__('keybridge').__file__)
        plan = plan_archive(self.db, 'archive-2026q4')
        self.assertEqual('ready', plan['status'])
        self.assertEqual(['A', 'B', 'C'], plan['selected'])
        self.assertEqual(6, plan['used_units'])
        report = archive_report(self.db)
        decisions = {x['candidate_id']: x['decision'] for x in report['plan_items']}
        self.assertEqual({'A': 'retain', 'B': 'retain', 'C': 'retain', 'D': 'defer'}, decisions)
        self.assertEqual('legal hold', next(x for x in report['plan_items'] if x['candidate_id'] == 'A')['reason'])

    def test_same_custody_domain_cannot_fake_quorum_and_conflict_blocks(self):
        plan_archive(self.db, 'archive-2026q4')
        import_replica(self.db, self.fixtures / 'archive_replica_good.json')
        import_replica(self.db, self._proof('proof-A-site1-repeat', 'A', 'site-1', 'sha-A'))
        first = audit_archive(self.db, 1)
        self.assertEqual('at_risk', first['status'])
        self.assertEqual('fragile', first['items'][0]['status'])
        import_replica(self.db, self.fixtures / 'archive_replica_conflict.json')
        second = audit_archive(self.db, 1)
        self.assertEqual('disputed', second['items'][0]['status'])
        self.assertEqual(['site-2'], second['items'][0]['conflict_domains'])
        self.assertEqual('fragile', first['items'][0]['status'])

    def test_independent_matching_domains_certify_all_selected_lots(self):
        plan_archive(self.db, 'archive-2026q4')
        for candidate in ('A', 'B', 'C'):
            for domain in ('site-1', 'site-2'):
                import_replica(self.db, self._proof(f'{candidate}-{domain}', candidate, domain, f'sha-{candidate}'))
        result = audit_archive(self.db, 1)
        self.assertEqual('recoverable', result['status'])
        self.assertEqual({'recoverable'}, {x['status'] for x in result['items']})
        with self.assertRaisesRegex(ValueError, 'not in retained'):
            import_replica(self.db, self._proof('D-site1', 'D', 'site-1', 'sha-D'))

    def test_new_candidate_revision_makes_old_plan_stale(self):
        plan_archive(self.db, 'archive-2026q4')
        path = self.root / 'new.json'
        path.write_text(json.dumps([{'id':'E','contract':'archive-2026q4','cohort':'south',
            'units':1,'value':10,'digest':'sha-E','legal_hold':False}]), encoding='utf-8')
        import_candidates(self.db, path)
        with self.assertRaisesRegex(ValueError, 'stale'):
            import_replica(self.db, self.fixtures / 'archive_replica_good.json')
        stale = audit_archive(self.db, 1)
        self.assertEqual('stale', stale['status'])
        second = plan_archive(self.db, 'archive-2026q4')
        self.assertIn('E', second['selected'])

    def test_impossible_legal_hold_and_coverage_block_plan(self):
        other = self.root / 'blocked.json'
        other.write_text(json.dumps({'id':'blocked','budget':1,'min_per_cohort':1,'replica_quorum':2}), encoding='utf-8')
        register_contract(self.db, other)
        lots = self.root / 'blocked-lots.json'
        lots.write_text(json.dumps([{'id':'H','contract':'blocked','cohort':'north','units':2,
            'value':100,'digest':'sha-H','legal_hold':True}]), encoding='utf-8')
        import_candidates(self.db, lots)
        result = plan_archive(self.db, 'blocked')
        self.assertEqual('blocked', result['status'])
        self.assertEqual([], result['selected'])


if __name__ == '__main__':
    unittest.main()
