"""Actual offline byte reconstruction, not receipt counting."""

import hashlib
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from keybridge.archive import import_candidates, plan_archive, register_contract
from keybridge.shards import create_manifest, ingest_shard, restore, shard_report
from keybridge.store import connect


class ShardArchiveTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'archive.db'
        self.raw = b'offline object: source CSV bytes\nrow A,1\nrow B,2\n'
        self.source = self.root / 'source.csv'
        self.source.write_bytes(self.raw)
        self._json('contract.json', {'id': 'shard-demo', 'budget': 2, 'min_per_cohort': 1, 'replica_quorum': 2})
        self._json('lots.json', [{'id': 'source-1', 'contract': 'shard-demo', 'cohort': 'north',
                                  'units': 1, 'value': 10, 'digest': hashlib.sha256(self.raw).hexdigest(),
                                  'legal_hold': True}])
        register_contract(self.db, self.root / 'contract.json')
        import_candidates(self.db, self.root / 'lots.json')
        self.plan = plan_archive(self.db, 'shard-demo')['plan_id']
        self.manifest = create_manifest(self.db, self.plan, 'source-1', 1, self.source, self.root / 'parts')

    def _json(self, name, payload):
        p = self.root / name
        p.write_text(json.dumps(payload), encoding='utf-8')
        return p

    def _add(self, index, domain, proof=None, path=None):
        return ingest_shard(self.db, self.manifest['manifest_id'], proof or f'{index}-{domain}',
                            index, domain, path or self.manifest['paths'][index])

    def test_each_distinct_pair_reconstructs_exact_bytes_after_one_domain_loss(self):
        print(__import__('keybridge').__file__)
        for index in range(3):
            self._add(index, f'domain-{index}')
        for lost in ('domain-0', 'domain-1', 'domain-2'):
            output = self.root / f'restored-{lost}.csv'
            result = restore(self.db, self.manifest['manifest_id'], [lost], output)
            self.assertEqual('restorable', result['status'])
            self.assertEqual(self.raw, output.read_bytes())
            self.assertEqual(hashlib.sha256(self.raw).hexdigest(), result['reconstructed_sha'])
        self.assertEqual(3, len(shard_report(self.db)['restores']))

    def test_duplicate_index_and_custody_domain_do_not_fake_recoverability(self):
        self._add(0, 'same', 'first')
        self._add(0, 'other', 'duplicate-index')
        self.assertEqual('insufficient', restore(self.db, self.manifest['manifest_id'])['status'])
        self._add(1, 'same')
        self.assertEqual('insufficient', restore(self.db, self.manifest['manifest_id'], ['other'])['status'])
        self._add(2, 'independent')
        self.assertEqual('restorable', restore(self.db, self.manifest['manifest_id'], ['other'])['status'])

    def test_conflicting_surviving_shard_blocks_restore_and_keeps_proof(self):
        self._add(0, 'first')
        self._add(1, 'second')
        bad = self.root / 'tampered.bin'
        bad.write_bytes(b'wrong bytes')
        self._add(2, 'third', 'bad-proof', bad)
        result = restore(self.db, self.manifest['manifest_id'])
        self.assertEqual('disputed', result['status'])
        self.assertIn('bad-proof', [r['id'] for r in shard_report(self.db)['proofs']])
        self.assertEqual('restorable', restore(self.db, self.manifest['manifest_id'], ['third'])['status'])

    def test_manifest_revision_and_plan_revision_stale_prior_recovery(self):
        self._add(0, 'first')
        self._add(1, 'second')
        self.assertEqual('restorable', restore(self.db, self.manifest['manifest_id'])['status'])
        next_manifest = create_manifest(self.db, self.plan, 'source-1', 2, self.source, self.root / 'parts')
        self.assertEqual('stale', restore(self.db, self.manifest['manifest_id'])['status'])
        with self.assertRaisesRegex(ValueError, 'stale'):
            create_manifest(self.db, self.plan, 'source-1', 1, self.source, self.root / 'parts')
        self.assertEqual('insufficient', restore(self.db, next_manifest['manifest_id'])['status'])
        self._json('new-lot.json', [{'id': 'source-2', 'contract': 'shard-demo', 'cohort': 'north',
                                    'units': 1, 'value': 1, 'digest': 'x', 'legal_hold': False}])
        import_candidates(self.db, self.root / 'new-lot.json')
        self.assertEqual('stale', restore(self.db, next_manifest['manifest_id'])['status'])

    def test_source_digest_and_proof_identity_are_bound(self):
        wrong = self.root / 'wrong.csv'
        wrong.write_bytes(b'different source object')
        with self.assertRaisesRegex(ValueError, 'object bytes'):
            create_manifest(self.db, self.plan, 'source-1', 2, wrong, self.root / 'other')
        self._add(0, 'first', 'same-id')
        self.assertTrue(self._add(0, 'first', 'same-id')['idempotent'])
        with self.assertRaisesRegex(ValueError, 'reused'):
            self._add(1, 'second', 'same-id')
        with closing(connect(self.db)) as db:
            self.assertEqual(1, db.execute('SELECT COUNT(*) FROM shard_proof').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
