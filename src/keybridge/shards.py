"""Offline 2-of-3 XOR archive shards with actual object reconstruction."""

import hashlib
import json
from contextlib import closing
from pathlib import Path

from .store import connect


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairwise_xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def _current_manifest(db, manifest_id):
    row = db.execute('SELECT m.*,p.status AS plan_status,p.revision AS plan_revision,'
                     'c.revision AS contract_revision,a.digest AS candidate_digest '
                     'FROM shard_manifest m JOIN archive_plan p ON p.id=m.plan_id '
                     'JOIN archive_contract c ON c.id=p.contract_id '
                     'JOIN archive_candidate a ON a.id=m.candidate_id WHERE m.id=?', (manifest_id,)).fetchone()
    if not row:
        raise ValueError('unknown shard manifest')
    newest = db.execute('SELECT MAX(revision) FROM shard_manifest WHERE plan_id=? AND candidate_id=?',
                        (row['plan_id'], row['candidate_id'])).fetchone()[0]
    return row, row['plan_status'] == 'ready' and row['plan_revision'] == row['contract_revision'] and row['revision'] == newest


def create_manifest(db_path, plan_id, candidate_id, revision, input_file, output_dir):
    raw = Path(input_file).read_bytes()
    if not raw:
        raise ValueError('empty archive object')
    width = (len(raw) + 1) // 2
    data0 = raw[:width].ljust(width, b'\0')
    data1 = raw[width:].ljust(width, b'\0')
    parts = [data0, data1, _pairwise_xor(data0, data1)]
    with closing(connect(db_path)) as db, db:
        plan = db.execute('SELECT p.*,c.revision AS current_revision FROM archive_plan p '
                          'JOIN archive_contract c ON c.id=p.contract_id WHERE p.id=?', (plan_id,)).fetchone()
        selected = db.execute("SELECT 1 FROM archive_plan_item WHERE plan_id=? AND candidate_id=? AND decision='retain'",
                              (plan_id, candidate_id)).fetchone()
        candidate = db.execute('SELECT digest FROM archive_candidate WHERE id=?', (candidate_id,)).fetchone()
        if not plan or plan['status'] != 'ready' or plan['revision'] != plan['current_revision'] or not selected:
            raise ValueError('archive plan missing, blocked, stale or candidate not retained')
        if candidate['digest'] != _sha(raw):
            raise ValueError('object bytes do not match retained candidate digest')
        if int(revision) < 1:
            raise ValueError('manifest revision must be positive')
        old = db.execute('SELECT * FROM shard_manifest WHERE plan_id=? AND candidate_id=? AND revision=?',
                         (plan_id, candidate_id, revision)).fetchone()
        hashes = [_sha(part) for part in parts]
        head = db.execute('SELECT MAX(revision) FROM shard_manifest WHERE plan_id=? AND candidate_id=?',
                          (plan_id, candidate_id)).fetchone()[0]
        if head is not None and int(revision) < head:
            raise ValueError('manifest revision is stale')
        if old:
            if (old['object_sha'], old['object_size'], old['part0_sha'], old['part1_sha'], old['parity_sha']) != (
                    _sha(raw), len(raw), *hashes):
                raise ValueError('manifest revision reused with changed object or encoding')
            manifest_id = old['id']
        else:
            if head is not None and int(revision) <= head:
                raise ValueError('manifest revision must advance')
            manifest_id = db.execute('INSERT INTO shard_manifest(plan_id,candidate_id,revision,object_sha,object_size,part0_sha,part1_sha,parity_sha) '
                                     'VALUES(?,?,?,?,?,?,?,?)',
                                     (plan_id, candidate_id, revision, _sha(raw), len(raw), *hashes)).lastrowid
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, part in enumerate(parts):
        path = destination / f'{candidate_id}.v{revision}.p{index}.bin'
        if path.exists() and path.read_bytes() != part:
            raise ValueError('shard output path contains different bytes')
        path.write_bytes(part)
        paths.append(str(path))
    return {'manifest_id': manifest_id, 'object_sha': _sha(raw), 'part_sha': hashes, 'paths': paths}


def ingest_shard(db_path, manifest_id, proof_id, part_index, domain, path):
    raw = Path(path).read_bytes()
    if not proof_id or not domain or int(part_index) not in (0, 1, 2):
        raise ValueError('invalid shard proof')
    digest = _sha(raw)
    with closing(connect(db_path)) as db, db:
        _, current = _current_manifest(db, manifest_id)
        if not current:
            raise ValueError('manifest or archive plan is stale')
        old = db.execute('SELECT * FROM shard_proof WHERE id=?', (proof_id,)).fetchone()
        if old:
            if (old['manifest_id'], old['part_index'], old['domain'], old['sha']) != (
                    manifest_id, int(part_index), domain, digest):
                raise ValueError('proof id reused with different shard evidence')
            return {'proof_id': proof_id, 'idempotent': True}
        db.execute('INSERT INTO shard_proof(id,manifest_id,part_index,domain,sha,raw) VALUES(?,?,?,?,?,?)',
                   (proof_id, manifest_id, int(part_index), domain, digest, raw))
    return {'proof_id': proof_id, 'part_index': int(part_index), 'domain': domain, 'sha': digest}


def restore(db_path, manifest_id, lost_domains=(), output=None):
    lost = sorted(set(lost_domains))
    with closing(connect(db_path)) as db, db:
        manifest, current = _current_manifest(db, manifest_id)
        proofs = db.execute('SELECT * FROM shard_proof WHERE manifest_id=? ORDER BY id', (manifest_id,)).fetchall()
        expected = (manifest['part0_sha'], manifest['part1_sha'], manifest['parity_sha'])
        usable = {}
        conflicts = []
        for proof in proofs:
            if proof['domain'] in lost:
                continue
            if proof['sha'] != expected[proof['part_index']]:
                conflicts.append(proof['id'])
            else:
                usable.setdefault(proof['part_index'], []).append(proof)
        selected = None
        reconstructed = None
        if not current:
            status, reason = 'stale', 'manifest or retention plan revision changed'
        elif conflicts:
            status, reason = 'disputed', 'conflicting shard bytes from surviving custody domain'
        else:
            for left, right in ((0, 1), (0, 2), (1, 2)):
                pair = next(((a, b) for a in usable.get(left, []) for b in usable.get(right, [])
                             if a['domain'] != b['domain']), None)
                if pair:
                    selected = (left, right)
                    parts = {left: pair[0]['raw'], right: pair[1]['raw']}
                    if len(parts[left]) != len(parts[right]):
                        status, reason = 'corrupt', 'shard lengths differ'
                        break
                    if selected == (0, 1):
                        reconstructed = parts[0] + parts[1]
                    elif selected == (0, 2):
                        reconstructed = parts[0] + _pairwise_xor(parts[0], parts[2])
                    else:
                        reconstructed = _pairwise_xor(parts[1], parts[2]) + parts[1]
                    reconstructed = reconstructed[:manifest['object_size']]
                    if _sha(reconstructed) == manifest['object_sha'] == manifest['candidate_digest']:
                        status, reason = 'restorable', 'distinct shard indexes and domains reconstructed exact object'
                    else:
                        status, reason = 'corrupt', 'reconstructed object hash differs from manifest'
                    break
            else:
                status, reason = 'insufficient', 'no two distinct shard indexes on independent surviving domains'
        output_path = None
        if status == 'restorable' and output:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() and target.read_bytes() != reconstructed:
                raise ValueError('restore output path contains different bytes')
            temporary = target.with_name(target.name + '.tmp')
            temporary.write_bytes(reconstructed)
            temporary.replace(target)
            output_path = str(target)
        audit_id = db.execute('INSERT INTO shard_restore(manifest_id,lost_domains,status,part_indexes,reconstructed_sha,reason,output_path) '
                              'VALUES(?,?,?,?,?,?,?)', (manifest_id, json.dumps(lost), status,
                               json.dumps(selected or []), _sha(reconstructed) if reconstructed is not None else None,
                               reason, output_path)).lastrowid
    return {'audit_id': audit_id, 'manifest_id': manifest_id, 'status': status, 'reason': reason,
            'lost_domains': lost, 'part_indexes': list(selected or []),
            'reconstructed_sha': _sha(reconstructed) if reconstructed is not None else None,
            'output_path': output_path}


def shard_report(db_path):
    with closing(connect(db_path)) as db:
        return {
            'manifests': [dict(x) for x in db.execute('SELECT * FROM shard_manifest ORDER BY id')],
            'proofs': [dict(x) for x in db.execute('SELECT id,manifest_id,part_index,domain,sha FROM shard_proof ORDER BY id')],
            'restores': [dict(x) for x in db.execute('SELECT * FROM shard_restore ORDER BY id')],
        }
