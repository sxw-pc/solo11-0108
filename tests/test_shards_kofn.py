"""Systematic MDS k-of-n shard encoding and backward-compatible legacy XOR."""

import hashlib
import itertools
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from keybridge.archive import import_candidates, plan_archive, register_contract
from keybridge.shards import (
    MDS_CODEC, XOR_CODEC, _invert, create_manifest, ingest_shard, restore, shard_report,
)
from keybridge.store import connect


class KofNShardTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root / 'archive.db'
        self.source = self.root / 'source.bin'
        self.counter = 0

    def _json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def _prepare(self, raw, candidate='source-1'):
        """Register a fresh one-lot contract so plans never accumulate candidates."""
        self.source.write_bytes(raw)
        self.counter += 1
        contract = f'contract-{self.counter}'
        self._json(f'contract-{self.counter}.json',
                   {'id': contract, 'budget': 2, 'min_per_cohort': 1, 'replica_quorum': 2})
        register_contract(self.db, self.root / f'contract-{self.counter}.json')
        path = self._json(f'lots-{self.counter}.json', [{
            'id': candidate, 'contract': contract, 'cohort': 'north', 'units': 1,
            'value': 10, 'digest': hashlib.sha256(raw).hexdigest(), 'legal_hold': True,
        }])
        import_candidates(self.db, path)
        return plan_archive(self.db, contract)['plan_id']

    def _manifest(self, raw, *, k=None, n=None, codec=None, revision=1, candidate='source-1',
                  plan=None, out='parts'):
        """``raw`` already lives at ``self.source``; ``plan=None`` creates its own plan."""
        plan = plan if plan is not None else self._prepare(raw, candidate)
        return create_manifest(self.db, plan, candidate, revision, self.source, self.root / out,
                               k=k, n=n, codec=codec)

    def _matrix(self, manifest):
        with closing(connect(self.db)) as db:
            row = db.execute('SELECT generator FROM shard_manifest WHERE id=?',
                             (manifest['manifest_id'],)).fetchone()
            return json.loads(row['generator'])

    def _ingest_all(self, manifest, domains=None, paths=None):
        domains = domains or [f'domain-{index}' for index in range(manifest['n'])]
        paths = paths or manifest['paths']
        for index in range(manifest['n']):
            ingest_shard(self.db, manifest['manifest_id'],
                         f'proof-{manifest["manifest_id"]}-{index}',
                         index, domains[index], paths[index])

    # --- encoding matrix properties -------------------------------------

    def test_generator_is_systematic_and_every_k_rows_are_invertible(self):
        for k, n in ((2, 3), (3, 6), (4, 9), (1, 32)):
            raw = bytes(range(1, k * 8 + 1))
            manifest = self._manifest(raw, k=k, n=n, codec=MDS_CODEC,
                                      candidate=f'gen-{k}-{n}', out=f'parts-{k}-{n}')
            matrix = self._matrix(manifest)
            identity = [[1 if row == column else 0 for column in range(k)] for row in range(k)]
            self.assertEqual(identity, matrix[:k])
            self.assertEqual(n, len(matrix))
            for rows in itertools.combinations(matrix, k):
                self.assertIsNotNone(_invert(rows), f'singular selection {rows} for {k}-of-{n}')
            self.assertIsNone(_invert([[1, 1], [1, 1]]))

    def test_storage_ratio_matches_profile(self):
        raw = bytes(range(256)) * 3  # 768 bytes, even for XOR and 3-way split
        legacy = self._manifest(raw, candidate='xor-lot', out='xor-parts')
        mds = self._manifest(raw, candidate='mds-lot', k=3, n=6, codec=MDS_CODEC,
                             out='mds-parts')
        self.assertEqual(len(raw) * 1.5,
                         sum(len(Path(path).read_bytes()) for path in legacy['paths']))
        self.assertEqual(len(raw) * 2.0,
                         sum(len(Path(path).read_bytes()) for path in mds['paths']))
        self.assertEqual((XOR_CODEC, 3), (legacy['codec_id'], len(legacy['paths'])))
        self.assertEqual((MDS_CODEC, 6), (mds['codec_id'], len(mds['paths'])))

    # --- round trips -----------------------------------------------------

    def test_round_trip_k2_n3_and_k3_n6_odd_and_even_lengths(self):
        lengths = (1, 2, 3, 4, 5, 6, 7, 11, 12, 13, 18)
        for k, n in ((2, 3), (3, 6)):
            for length in lengths:
                raw = bytes((index * 37 + length) % 251 for index in range(length))
                manifest = self._manifest(raw, k=k, n=n, codec=MDS_CODEC,
                                          candidate=f'rt-{k}-{n}-{length}',
                                          out=f'rt-{k}-{n}-{length}')
                self._ingest_all(manifest)
                result = restore(self.db, manifest['manifest_id'])
                self.assertEqual('restorable', result['status'], (k, n, length, result))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), result['reconstructed_sha'])
                self.assertEqual(k, len(result['part_indexes']))
                self.assertEqual((k, n), (result['k'], result['n']))
                with closing(connect(self.db)) as db, db:
                    db.execute('DELETE FROM shard_proof WHERE manifest_id=?',
                               (manifest['manifest_id'],))

    def test_every_k_index_combination_recovers_after_domain_losses(self):
        raw = b'MDS object bytes that span several shards, with a padded tail.'
        for k, n in ((2, 3), (3, 6)):
            manifest = self._manifest(raw, k=k, n=n, codec=MDS_CODEC,
                                      candidate=f'combo-{k}-{n}', out=f'combo-{k}-{n}')
            self._ingest_all(manifest)
            domains = [f'domain-{index}' for index in range(manifest['n'])]
            seen = set()
            expected = len(list(itertools.combinations(range(n), k)))
            for combination in itertools.combinations(range(n), k):
                kept = {domains[index] for index in combination}
                lost = [domain for domain in domains if domain not in kept]
                result = restore(self.db, manifest['manifest_id'], lost)
                self.assertEqual('restorable', result['status'], (k, n, combination, result))
                self.assertEqual(list(combination), sorted(result['part_indexes']))
                self.assertEqual(k, len(result['part_indexes']))
                self.assertEqual(hashlib.sha256(raw).hexdigest(), result['reconstructed_sha'])
                seen.add(tuple(sorted(result['part_indexes'])))
            self.assertEqual(expected, len(seen))
            with closing(connect(self.db)) as db, db:
                db.execute('DELETE FROM shard_proof WHERE manifest_id=?',
                           (manifest['manifest_id'],))

    def test_three_of_six_survives_loss_of_three_domains_while_xor_does_not(self):
        raw = b'budget profile comparison object' * 3
        legacy = self._manifest(raw, candidate='xor-lot', out='xor-parts')
        mds = self._manifest(raw, candidate='mds-lot', k=3, n=6, codec=MDS_CODEC,
                             out='mds-parts')
        self._ingest_all(legacy)
        self._ingest_all(mds)
        lost = ['domain-0', 'domain-1', 'domain-2']
        mds_result = restore(self.db, mds['manifest_id'], lost)
        self.assertEqual('restorable', mds_result['status'])
        self.assertEqual([3, 4, 5], mds_result['part_indexes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), mds_result['reconstructed_sha'])
        # The legacy 2-of-3 manifest has only one shard left after those losses.
        self.assertEqual('insufficient', restore(self.db, legacy['manifest_id'], lost)['status'])

    def test_single_domain_loss_restores_both_profiles(self):
        raw = b'one domain down'
        legacy = self._manifest(raw, candidate='xor-lot', out='xor-one')
        mds = self._manifest(raw, candidate='mds-lot', k=3, n=6, codec=MDS_CODEC,
                             out='mds-one')
        self._ingest_all(legacy)
        self._ingest_all(mds)
        for manifest in (legacy, mds):
            output = self.root / f'out-{manifest["manifest_id"]}.bin'
            result = restore(self.db, manifest['manifest_id'], ['domain-1'], output)
            self.assertEqual('restorable', result['status'])
            self.assertEqual(raw, output.read_bytes())

    # --- proof integrity -------------------------------------------------

    def test_duplicate_index_and_shared_domain_fail_rank_and_domain_check(self):
        raw = b'duplicate proof evidence'
        manifest = self._manifest(raw, k=3, n=6, codec=MDS_CODEC)
        mid = manifest['manifest_id']
        ingest_shard(self.db, mid, 'dup-a', 0, 'site-a', manifest['paths'][0])
        ingest_shard(self.db, mid, 'dup-b', 0, 'site-b', manifest['paths'][0])
        ingest_shard(self.db, mid, 'p1', 1, 'site-a', manifest['paths'][1])
        ingest_shard(self.db, mid, 'p2', 2, 'site-a', manifest['paths'][2])
        # Index 0 lives on two domains but the other indexes only on site-a.
        # Three distinct indexes exist, but only two independent domains survive.
        self.assertEqual('insufficient', restore(self.db, mid, ['site-b'])['status'])
        # Dropping site-a leaves index 0 twice on site-b; still only one slot.
        self.assertEqual('insufficient', restore(self.db, mid, ['site-a'])['status'])
        # Two more indexes on independent domains give a decodable {0,3,4} set.
        ingest_shard(self.db, mid, 'p3', 3, 'site-c', manifest['paths'][3])
        ingest_shard(self.db, mid, 'p4', 4, 'site-d', manifest['paths'][4])
        result = restore(self.db, mid, ['site-b'])
        self.assertEqual('restorable', result['status'])
        self.assertEqual([0, 3, 4], result['part_indexes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['reconstructed_sha'])
        # Restoring without losses still sees three site-a proofs for indexes 0/1/2;
        # a valid assignment must reach site-c/site-d instead.
        self.assertEqual('restorable', restore(self.db, mid)['status'])

    def test_bad_shard_is_disputed_and_excluding_its_domain_clears_it(self):
        raw = b'bad shard detection'
        manifest = self._manifest(raw, k=3, n=6, codec=MDS_CODEC)
        self._ingest_all(manifest)
        bad = self.root / 'bad.bin'
        bad.write_bytes(b'not the encoded shard')
        ingest_shard(self.db, manifest['manifest_id'], 'bad-proof', 4, 'domain-4', bad)
        self.assertEqual('disputed', restore(self.db, manifest['manifest_id'])['status'])
        self.assertEqual('restorable',
                         restore(self.db, manifest['manifest_id'], ['domain-4'])['status'])

    def test_whole_object_digest_conflict_with_candidate_is_corrupt(self):
        raw = b'manifest and candidate disagree'
        manifest = self._manifest(raw, k=2, n=3, codec=MDS_CODEC)
        self._ingest_all(manifest)
        # Keep the plan current but make the candidate digest disagree with the manifest.
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE archive_candidate SET digest=? WHERE id='source-1'",
                       (hashlib.sha256(b'conflicting candidate digest').hexdigest(),))
            db.execute('UPDATE archive_contract SET revision=revision+1')
            db.execute('UPDATE archive_plan SET revision=(SELECT revision FROM archive_contract)')
        result = restore(self.db, manifest['manifest_id'])
        self.assertEqual('corrupt', result['status'])
        self.assertIn('candidate', result['reason'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['reconstructed_sha'])

    def test_manifest_revision_is_immutable_and_new_revision_makes_old_stale(self):
        raw = b'revision contract bytes'
        plan = self._prepare(raw)
        first = self._manifest(raw, plan=plan, k=3, n=6, codec=MDS_CODEC, revision=1)
        self._ingest_all(first)
        self.assertEqual('restorable', restore(self.db, first['manifest_id'])['status'])
        with self.assertRaisesRegex(ValueError, 'changed object or encoding'):
            create_manifest(self.db, plan, 'source-1', 1, self.source, self.root / 'again',
                            k=2, n=3, codec=MDS_CODEC)
        second = self._manifest(raw, plan=plan, k=3, n=6, codec=MDS_CODEC, revision=2,
                                out='parts-v2')
        self.assertEqual('stale', restore(self.db, first['manifest_id'])['status'])
        self.assertEqual('insufficient', restore(self.db, second['manifest_id'])['status'])
        # The same bytes, codec and revision replay idempotently.
        replay = create_manifest(self.db, plan, 'source-1', 2, self.source,
                                 self.root / 'parts-v2', k=3, n=6, codec=MDS_CODEC)
        self.assertEqual(second['manifest_id'], replay['manifest_id'])
        self.assertEqual(second['part_sha'], replay['part_sha'])

    def test_encode_parameter_validation(self):
        self._prepare(b'validation bytes')
        for kwargs, message in (
            ({'k': 0, 'n': 3, 'codec': MDS_CODEC}, '1 <= k < n'),
            ({'k': 3, 'n': 3, 'codec': MDS_CODEC}, '1 <= k < n'),
            ({'k': 2, 'n': 33, 'codec': MDS_CODEC}, '1 <= k < n'),
            ({'k': 2, 'n': 3, 'codec': 'unknown-codec'}, 'unknown shard codec'),
            ({'k': 3, 'n': 6, 'codec': XOR_CODEC}, 'xor-2of3'),
            ({'k': 3, 'codec': MDS_CODEC}, 'requires k and n'),
        ):
            with self.assertRaisesRegex(ValueError, message):
                create_manifest(self.db, 1, 'source-1', 1, self.source, self.root / 'bad',
                                **kwargs)
        self.assertFalse((self.root / 'bad').exists())

    def test_ingest_rejects_out_of_range_index_for_new_codec(self):
        manifest = self._manifest(b'index range check', k=3, n=6, codec=MDS_CODEC)
        with self.assertRaisesRegex(ValueError, 'invalid shard proof'):
            ingest_shard(self.db, manifest['manifest_id'], 'far', 6, 'domain-x',
                         manifest['paths'][0])
        with closing(connect(self.db)) as db:
            self.assertEqual(0, db.execute('SELECT COUNT(*) FROM shard_proof').fetchone()[0])

    def test_singular_surviving_rows_yield_insufficient_not_fake_recovery(self):
        raw = b'rank check on surviving rows'
        manifest = self._manifest(raw, k=3, n=6, codec=MDS_CODEC)
        mid = manifest['manifest_id']
        matrix = self._matrix(manifest)
        matrix[1] = list(matrix[0])  # duplicate a row so {0,1,2} is singular
        with closing(connect(self.db)) as db, db:
            db.execute('UPDATE shard_manifest SET generator=? WHERE id=?',
                       (json.dumps(matrix, separators=(',', ':')), mid))
        for index in range(3):
            ingest_shard(self.db, mid, f'survivor-{index}', index, f'domain-{index}',
                         manifest['paths'][index])
        result = restore(self.db, mid)
        self.assertEqual('insufficient', result['status'])
        self.assertIn('invertible', result['reason'])

    def test_singular_sorted_prefix_falls_through_to_later_full_rank_rows(self):
        raw = b'fall through to later matrix rows'
        manifest = self._manifest(raw, k=3, n=6, codec=MDS_CODEC)
        mid = manifest['manifest_id']
        matrix = self._matrix(manifest)
        matrix[2] = list(matrix[0])  # {0,1,2} singular, {0,1,3} still full rank
        with closing(connect(self.db)) as db, db:
            db.execute('UPDATE shard_manifest SET generator=? WHERE id=?',
                       (json.dumps(matrix, separators=(',', ':')), mid))
        # Re-encode with the tampered matrix so shard bytes agree with the frozen rows.
        import keybridge.shards as shards_module
        chunks, width = shards_module._split_object(raw, 3)
        parts = [shards_module._linear_combine(row, chunks, width) for row in matrix]
        digests = [hashlib.sha256(part).hexdigest() for part in parts]
        with closing(connect(self.db)) as db, db:
            db.execute('UPDATE shard_manifest SET shard_sha=? WHERE id=?',
                       (json.dumps(digests, separators=(',', ':')), mid))
        for index, part in enumerate(parts):
            path = self.root / f'tampered-{index}.bin'
            path.write_bytes(part)
            ingest_shard(self.db, mid, f'tampered-{index}', index, f'domain-{index}', path)
        result = restore(self.db, mid)
        self.assertEqual('restorable', result['status'])
        self.assertEqual([0, 1, 3], result['part_indexes'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), result['reconstructed_sha'])

    # --- legacy compatibility -------------------------------------------

    def test_legacy_three_column_manifest_still_restores(self):
        raw = b'legacy two of three XOR object\nwith a second line\n'
        digest = hashlib.sha256(raw).hexdigest()
        width = (len(raw) + 1) // 2
        data0 = raw[:width].ljust(width, b'\0')
        data1 = raw[width:].ljust(width, b'\0')
        parity = bytes(left ^ right for left, right in zip(data0, data1))
        parts = [data0, data1, parity]
        hashes = [hashlib.sha256(part).hexdigest() for part in parts]
        old_db = self.root / 'old-schema.db'
        old = sqlite3.connect(old_db)
        old.executescript("""
            CREATE TABLE archive_contract(id TEXT PRIMARY KEY,budget INTEGER,min_per_cohort INTEGER,replica_quorum INTEGER,revision INTEGER);
            CREATE TABLE archive_candidate(id TEXT PRIMARY KEY,contract_id TEXT,cohort TEXT,units INTEGER,value INTEGER,digest TEXT,legal_hold INTEGER,source_sha TEXT,raw BLOB);
            CREATE TABLE archive_plan(id INTEGER PRIMARY KEY,contract_id TEXT,revision INTEGER,status TEXT,used_units INTEGER,total_value INTEGER,reason TEXT,created_at TEXT);
            CREATE TABLE archive_plan_item(plan_id INTEGER,candidate_id TEXT,decision TEXT,reason TEXT,PRIMARY KEY(plan_id,candidate_id));
            CREATE TABLE shard_manifest(id INTEGER PRIMARY KEY,plan_id INTEGER,candidate_id TEXT,revision INTEGER,object_sha TEXT,object_size INTEGER,part0_sha TEXT,part1_sha TEXT,parity_sha TEXT,created_at TEXT,UNIQUE(plan_id,candidate_id,revision));
            CREATE TABLE shard_proof(id TEXT PRIMARY KEY,manifest_id INTEGER,part_index INTEGER,domain TEXT,sha TEXT,raw BLOB,created_at TEXT);
            CREATE TABLE shard_restore(id INTEGER PRIMARY KEY,manifest_id INTEGER,lost_domains TEXT,status TEXT,part_indexes TEXT,reconstructed_sha TEXT,reason TEXT,output_path TEXT,created_at TEXT);
        """)
        old.execute('INSERT INTO archive_contract VALUES(?,?,?,?,?)', ('old-demo', 2, 1, 2, 1))
        old.execute('INSERT INTO archive_candidate VALUES(?,?,?,?,?,?,?,?,?)',
                    ('legacy-1', 'old-demo', 'north', 1, 10, digest, 1, digest, raw))
        old.execute('INSERT INTO archive_plan VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                    (1, 'old-demo', 1, 'ready', 1, 10, 'legacy'))
        old.execute("INSERT INTO archive_plan_item VALUES(1,'legacy-1','retain','legacy')")
        old.execute('INSERT INTO shard_manifest(id,plan_id,candidate_id,revision,object_sha,object_size,'
                    'part0_sha,part1_sha,parity_sha) VALUES(1,1,?,1,?,?,?,?,?)',
                    ('legacy-1', digest, len(raw), *hashes))
        for index, (domain, part) in enumerate(zip(('a', 'b', 'c'), parts)):
            old.execute('INSERT INTO shard_proof VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)',
                        (f'old-{index}', 1, index, domain, hashes[index], part))
        old.commit()
        old.close()

        output = self.root / 'legacy-out.bin'
        result = restore(old_db, 1, ['c'], output)
        self.assertEqual('restorable', result['status'])
        self.assertEqual(XOR_CODEC, result['codec_id'])
        self.assertEqual((2, 3), (result['k'], result['n']))
        self.assertEqual([0, 1], result['part_indexes'])
        self.assertEqual(raw, output.read_bytes())
        # connect() adds the frozen encoding columns without rewriting the row.
        with closing(connect(old_db)) as db:
            row = db.execute('SELECT * FROM shard_manifest WHERE id=1').fetchone()
            self.assertIsNone(row['codec_id'])
            self.assertIsNone(row['k'])
        report = shard_report(old_db)
        self.assertEqual((XOR_CODEC, 2, 3), (report['manifests'][0]['codec_id'],
                                             report['manifests'][0]['k'],
                                             report['manifests'][0]['n']))
        self.assertEqual(3, len(report['manifests'][0]['shard_sha']))

    def test_report_lists_encoding_parameters_for_both_manifest_classes(self):
        raw = b'report both codecs'
        plan = self._prepare(raw)
        legacy = self._manifest(raw, plan=plan, out='report-xor')
        self._ingest_all(legacy)
        self.assertEqual('restorable', restore(self.db, legacy['manifest_id'], ['domain-1'])['status'])
        mds = self._manifest(raw, plan=plan, k=3, n=6, codec=MDS_CODEC, revision=2,
                             out='report-mds')
        self._ingest_all(mds)
        self.assertEqual('restorable', restore(self.db, mds['manifest_id'], ['domain-1'])['status'])
        report = shard_report(self.db)
        by_id = {item['id']: item for item in report['manifests']}
        self.assertEqual((XOR_CODEC, 2, 3),
                         (by_id[legacy['manifest_id']]['codec_id'], by_id[legacy['manifest_id']]['k'],
                          by_id[legacy['manifest_id']]['n']))
        self.assertEqual((MDS_CODEC, 3, 6),
                         (by_id[mds['manifest_id']]['codec_id'], by_id[mds['manifest_id']]['k'],
                          by_id[mds['manifest_id']]['n']))
        self.assertEqual(3, len(by_id[legacy['manifest_id']]['shard_sha']))
        self.assertEqual(6, len(by_id[mds['manifest_id']]['shard_sha']))
        self.assertEqual(6, len(by_id[mds['manifest_id']]['encoding']['generator']))
        for restored in report['restores']:
            self.assertIn(restored['codec_id'], (XOR_CODEC, MDS_CODEC))
            self.assertIn(restored['k'], (2, 3))
            self.assertIn(restored['n'], (3, 6))
            self.assertIsInstance(restored['part_indexes'], list)
            self.assertEqual(64, len(restored['reconstructed_sha']))

    def test_proof_is_bound_to_manifest_revision(self):
        raw = b'proof revision binding'
        manifest = self._manifest(raw, k=3, n=6, codec=MDS_CODEC)
        self._ingest_all(manifest)
        with closing(connect(self.db)) as db, db:
            db.execute("UPDATE shard_proof SET revision=99 WHERE id='proof-1-0'")
        self.assertEqual('disputed', restore(self.db, manifest['manifest_id'])['status'])
        # A proof id cannot be replayed against the same manifest with a new revision claim.
        with self.assertRaisesRegex(ValueError, 'reused'):
            ingest_shard(self.db, manifest['manifest_id'], 'proof-1-0', 0, 'domain-0',
                         manifest['paths'][0])

    def test_manifest_points_at_retained_plan_item_and_candidate_digest(self):
        raw = b'plan binding bytes'
        plan = self._prepare(raw)
        wrong = self.root / 'other.bin'
        wrong.write_bytes(b'another object entirely')
        with self.assertRaisesRegex(ValueError, 'object bytes'):
            create_manifest(self.db, plan, 'source-1', 1, wrong, self.root / 'wrong-out')
        with self.assertRaisesRegex(ValueError, 'not retained'):
            create_manifest(self.db, plan, 'ghost', 1, self.source, self.root / 'ghost-out')
        with closing(connect(self.db)) as db, db:
            db.execute('UPDATE archive_contract SET revision=revision+1')
        with self.assertRaisesRegex(ValueError, 'stale'):
            create_manifest(self.db, plan, 'source-1', 1, self.source, self.root / 'stale-out')

    def test_cli_accepts_codec_k_and_n(self):
        raw = b'cli encoded object bytes'
        plan = self._prepare(raw)
        env = dict(os.environ)
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent.parent / 'src')
        command = [sys.executable, '-m', 'keybridge', 'shard-manifest', '--db', str(self.db),
                   '--plan', str(plan), '--candidate', 'source-1', '--revision', '1',
                   '--input', str(self.source), '--out', str(self.root / 'cli-parts'),
                   '--codec', MDS_CODEC, '--k', '3', '--n', '6']
        completed = subprocess.run(command, capture_output=True, text=True, env=env)
        self.assertEqual(0, completed.returncode, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual((MDS_CODEC, 3, 6), (payload['codec_id'], payload['k'], payload['n']))
        self.assertEqual(6, len(payload['paths']))


if __name__ == '__main__':
    unittest.main()
