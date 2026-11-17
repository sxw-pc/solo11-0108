"""Offline archive shards: legacy 2-of-3 XOR plus systematic MDS k-of-n.

Legacy manifests created before the general encoding contract carry only the
three ``part0_sha``/``part1_sha``/``parity_sha`` hashes and are still restored
with the original two-data-part XOR algorithm.  New manifests freeze a codec
id, k, n, the GF(256) generator matrix and one digest per shard, so a recovery
can only use shard indexes whose matrix rows are actually invertible.
"""

import hashlib
import json
from contextlib import closing
from pathlib import Path

from .store import connect

XOR_CODEC = 'xor-2of3'
MDS_CODEC = 'mds-vandermonde-gf256'
XOR_GENERATOR = [[1, 0], [0, 1], [1, 1]]

# GF(256) arithmetic modulo x^8 + x^4 + x^3 + x + 1 (the Rijndael polynomial).
_EXP = [0] * 512
_LOG = [0] * 256
_value = 1
for _power in range(255):
    _EXP[_power] = _value
    _LOG[_value] = _power
    _value <<= 1
    if _value & 0x100:
        _value ^= 0x11D
for _power in range(255, 512):
    _EXP[_power] = _EXP[_power - 255]
_MULTIPLICATION_TABLES = {}


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairwise_xor(left, right):
    return bytes(a ^ b for a, b in zip(left, right))


def _gf_mul(left, right):
    if left == 0 or right == 0:
        return 0
    return _EXP[_LOG[left] + _LOG[right]]


def _gf_inverse(value):
    if value == 0:
        raise ZeroDivisionError('zero has no multiplicative inverse in GF(256)')
    return _EXP[255 - _LOG[value]]


def _gf_power(value, exponent):
    result = 1
    base = value
    while exponent:
        if exponent & 1:
            result = _gf_mul(result, base)
        base = _gf_mul(base, base)
        exponent >>= 1
    return result


def _multiplication_table(coefficient):
    table = _MULTIPLICATION_TABLES.get(coefficient)
    if table is None:
        table = bytes(_gf_mul(coefficient, value) for value in range(256))
        _MULTIPLICATION_TABLES[coefficient] = table
    return table


def _linear_combine(coefficients, chunks, width):
    """Return sum(coefficient * chunk) over GF(256)."""
    result = 0
    for coefficient, chunk in zip(coefficients, chunks):
        if coefficient == 0:
            continue
        scaled = chunk if coefficient == 1 else chunk.translate(_multiplication_table(coefficient))
        result ^= int.from_bytes(scaled, 'big')
    return result.to_bytes(width, 'big')


def _invert(matrix):
    """Gauss-Jordan inverse over GF(256); return None when singular."""
    size = len(matrix)
    if any(len(row) != size for row in matrix):
        return None
    augmented = [list(row) + [1 if row_no == column else 0 for column in range(size)]
                 for row_no, row in enumerate(matrix)]
    for column in range(size):
        pivot = next((row_no for row_no in range(column, size) if augmented[row_no][column]), None)
        if pivot is None:
            return None
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        scale = _gf_inverse(augmented[column][column])
        augmented[column] = [_gf_mul(value, scale) for value in augmented[column]]
        for row_no in range(size):
            if row_no != column and augmented[row_no][column]:
                factor = augmented[row_no][column]
                augmented[row_no] = [value ^ _gf_mul(factor, pivot_value)
                                     for value, pivot_value in zip(augmented[row_no], augmented[column])]
    return [row[size:] for row in augmented]


def _matrix_multiply(left, right):
    columns = len(right[0]) if right else 0
    return [[_linear_combine_row(left[row_no], right, column) for column in range(columns)]
            for row_no in range(len(left))]


def _linear_combine_row(left_row, right, column):
    result = 0
    for position in range(len(left_row)):
        if left_row[position] and right[position][column]:
            result ^= _gf_mul(left_row[position], right[position][column])
    return result


def _mds_generator(k, n):
    """Systematic n x k Vandermonde matrix; every k rows are invertible.

    ``x_i = i + 1`` is distinct and nonzero in GF(256) for n <= 32, so the
    Vandermonde matrix is MDS.  Right-multiplying by the inverse of its top
    k x k block makes the first k rows the identity without losing that
    property.
    """
    vandermonde = [[_gf_power(row + 1, power) for power in range(k)] for row in range(n)]
    top_inverse = _invert(vandermonde[:k])
    if top_inverse is None:  # pragma: no cover - mathematically unreachable for k<=32
        raise ValueError('cannot build an invertible systematic MDS matrix')
    return _matrix_multiply(vandermonde, top_inverse)


def _split_object(raw, k):
    width = (len(raw) + k - 1) // k
    return [raw[start:start + width].ljust(width, b'\0') for start in range(0, width * k, width)], width


def _encode(raw, k, matrix):
    chunks, width = _split_object(raw, k)
    return [_linear_combine(row, chunks, width) for row in matrix]


def _reconstruct(inverse, shards, width):
    return b''.join(_linear_combine(row, shards, width) for row in inverse)


def _match_domains(usable):
    """Maximum index->proof matching where every chosen proof has its own domain."""
    matched = {}
    domain_index = {}
    ordered = sorted(usable)

    def augment(index, seen):
        for proof in sorted(usable[index], key=lambda proof: (proof['domain'], proof['id'])):
            domain = proof['domain']
            if domain in seen:
                continue
            seen.add(domain)
            previous = domain_index.get(domain)
            if previous is None or augment(previous, seen):
                domain_index[domain] = index
                matched[index] = proof
                return True
        return False

    for index in ordered:
        augment(index, set())
    return matched


def _rank(rows):
    working = [list(row) for row in rows]
    width = len(working[0]) if working else 0
    rank = 0
    for column in range(width):
        pivot = next((row_no for row_no in range(rank, len(working)) if working[row_no][column]), None)
        if pivot is None:
            continue
        working[rank], working[pivot] = working[pivot], working[rank]
        scale = _gf_inverse(working[rank][column])
        working[rank] = [_gf_mul(value, scale) for value in working[rank]]
        for row_no in range(len(working)):
            if row_no != rank and working[row_no][column]:
                factor = working[row_no][column]
                working[row_no] = [value ^ _gf_mul(factor, pivot_value)
                                   for value, pivot_value in zip(working[row_no], working[rank])]
        rank += 1
        if rank == len(working):
            break
    return rank


def _independent_rows(matrix, indexes, k):
    """First k indexes (in order) whose matrix rows are linearly independent."""
    chosen, rows = [], []
    for index in indexes:
        candidate = rows + [matrix[index]]
        if _rank(candidate) > len(rows):
            chosen.append(index)
            rows = candidate
            if len(chosen) == k:
                return chosen
    return None


def _resolve_codec(codec, k, n):
    if codec is None:
        if k is None and n is None:
            codec = XOR_CODEC
        elif k is None or n is None:
            raise ValueError('both k and n are required for a systematic MDS codec')
        else:
            codec = MDS_CODEC
    codec = str(codec)
    if codec == XOR_CODEC:
        if k not in (None, 2) or n not in (None, 3):
            raise ValueError('xor-2of3 requires k=2 and n=3')
        return XOR_CODEC, 2, 3, [row[:] for row in XOR_GENERATOR]
    if codec == MDS_CODEC:
        if k is None or n is None:
            raise ValueError('systematic MDS codec requires k and n')
        try:
            k, n = int(k), int(n)
        except (TypeError, ValueError):
            raise ValueError('k and n must be integers') from None
        if not 1 <= k < n <= 32:
            raise ValueError('systematic MDS requires 1 <= k < n <= 32')
        return MDS_CODEC, k, n, _mds_generator(k, n)
    raise ValueError('unknown shard codec')


def _canonical(value):
    return json.dumps(value, separators=(',', ':'))


def _manifest_codec(row):
    codec = row['codec_id'] or XOR_CODEC
    if codec == XOR_CODEC:
        return codec, 2, 3
    if codec == MDS_CODEC:
        if row['k'] is None or row['n'] is None:
            raise ValueError('manifest is missing its k and n')
        k, n = int(row['k']), int(row['n'])
        if not 1 <= k < n <= 32:
            raise ValueError('manifest has invalid k and n')
        return codec, k, n
    raise ValueError('unsupported shard codec')


def _manifest_generator(row):
    codec, k, n = _manifest_codec(row)
    if row['generator']:
        try:
            matrix = json.loads(row['generator'])
        except json.JSONDecodeError:
            raise ValueError('manifest encoding matrix is not valid JSON') from None
        if (not isinstance(matrix, list) or len(matrix) != n
                or any(not isinstance(part, list) or len(part) != k for part in matrix)
                or any(not isinstance(value, int) or not 0 <= value <= 255
                       for part in matrix for value in part)):
            raise ValueError('manifest encoding matrix has wrong dimensions or coefficients')
        return matrix
    if codec != XOR_CODEC:
        raise ValueError('manifest is missing its encoding matrix')
    return [part[:] for part in XOR_GENERATOR]


def _manifest_shards(row):
    codec, _, n = _manifest_codec(row)
    if row['shard_sha']:
        try:
            digests = json.loads(row['shard_sha'])
        except json.JSONDecodeError:
            raise ValueError('manifest shard digests are not valid JSON') from None
        if not isinstance(digests, list) or len(digests) != n:
            raise ValueError('manifest shard digest list has wrong length')
        return digests
    if codec != XOR_CODEC:
        raise ValueError('manifest is missing shard digests')
    return [row['part0_sha'], row['part1_sha'], row['parity_sha']]


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


def create_manifest(db_path, plan_id, candidate_id, revision, input_file, output_dir,
                    *, k=None, n=None, codec=None):
    codec_id, k, n, matrix = _resolve_codec(codec, k, n)
    raw = Path(input_file).read_bytes()
    if not raw:
        raise ValueError('empty archive object')
    if codec_id == XOR_CODEC:
        width = (len(raw) + 1) // 2
        first = raw[:width].ljust(width, b'\0')
        second = raw[width:].ljust(width, b'\0')
        parts = [first, second, _pairwise_xor(first, second)]
    else:
        parts = _encode(raw, k, matrix)
    hashes = [_sha(part) for part in parts]
    generator = _canonical(matrix)
    shard_sha = _canonical(hashes)
    object_sha = _sha(raw)
    part0_sha, part1_sha, parity_sha = (hashes + ['', '', ''])[:3]
    with closing(connect(db_path)) as db, db:
        plan = db.execute('SELECT p.*,c.revision AS current_revision FROM archive_plan p '
                          'JOIN archive_contract c ON c.id=p.contract_id WHERE p.id=?', (plan_id,)).fetchone()
        selected = db.execute("SELECT 1 FROM archive_plan_item WHERE plan_id=? AND candidate_id=? AND decision='retain'",
                              (plan_id, candidate_id)).fetchone()
        candidate = db.execute('SELECT digest FROM archive_candidate WHERE id=?', (candidate_id,)).fetchone()
        if not plan or plan['status'] != 'ready' or plan['revision'] != plan['current_revision'] or not selected:
            raise ValueError('archive plan missing, blocked, stale or candidate not retained')
        if candidate['digest'] != object_sha:
            raise ValueError('object bytes do not match retained candidate digest')
        if int(revision) < 1:
            raise ValueError('manifest revision must be positive')
        old = db.execute('SELECT * FROM shard_manifest WHERE plan_id=? AND candidate_id=? AND revision=?',
                         (plan_id, candidate_id, revision)).fetchone()
        head = db.execute('SELECT MAX(revision) FROM shard_manifest WHERE plan_id=? AND candidate_id=?',
                          (plan_id, candidate_id)).fetchone()[0]
        if head is not None and int(revision) < head:
            raise ValueError('manifest revision is stale')
        if old:
            old_codec, old_k, old_n = _manifest_codec(old)
            frozen = (old['object_sha'], int(old['object_size']), old_codec, old_k, old_n,
                      _canonical(_manifest_generator(old)), _canonical(_manifest_shards(old)))
            if frozen != (object_sha, len(raw), codec_id, k, n, generator, shard_sha):
                raise ValueError('manifest revision reused with changed object or encoding')
            manifest_id = old['id']
        else:
            if head is not None and int(revision) <= head:
                raise ValueError('manifest revision must advance')
            manifest_id = db.execute(
                'INSERT INTO shard_manifest(plan_id,candidate_id,revision,object_sha,object_size,'
                'part0_sha,part1_sha,parity_sha,codec_id,k,n,generator,shard_sha) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (plan_id, candidate_id, revision, object_sha, len(raw), part0_sha, part1_sha, parity_sha,
                 codec_id, k, n, generator, shard_sha)).lastrowid
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, part in enumerate(parts):
        path = destination / f'{candidate_id}.v{revision}.p{index}.bin'
        if path.exists() and path.read_bytes() != part:
            raise ValueError('shard output path contains different bytes')
        path.write_bytes(part)
        paths.append(str(path))
    return {'manifest_id': manifest_id, 'object_sha': object_sha, 'object_size': len(raw),
            'codec_id': codec_id, 'k': k, 'n': n, 'part_sha': hashes, 'shard_sha': hashes,
            'paths': paths}


def ingest_shard(db_path, manifest_id, proof_id, part_index, domain, path):
    raw = Path(path).read_bytes()
    digest = _sha(raw)
    with closing(connect(db_path)) as db, db:
        manifest, current = _current_manifest(db, manifest_id)
        _, _, n = _manifest_codec(manifest)
        try:
            part_index = int(part_index)
        except (TypeError, ValueError):
            raise ValueError('invalid shard proof') from None
        if not proof_id or not domain or part_index < 0 or part_index >= n:
            raise ValueError('invalid shard proof')
        if not current:
            raise ValueError('manifest or archive plan is stale')
        old = db.execute('SELECT * FROM shard_proof WHERE id=?', (proof_id,)).fetchone()
        if old:
            if (old['manifest_id'], old['part_index'], old['domain'], old['sha']) != (
                    manifest_id, part_index, domain, digest):
                raise ValueError('proof id reused with different shard evidence')
            if old['revision'] is not None and old['revision'] != manifest['revision']:
                raise ValueError('proof id reused with different shard evidence')
            return {'proof_id': proof_id, 'idempotent': True}
        db.execute('INSERT INTO shard_proof(id,manifest_id,revision,part_index,domain,sha,raw) VALUES(?,?,?,?,?,?,?)',
                   (proof_id, manifest_id, manifest['revision'], part_index, domain, digest, raw))
    return {'proof_id': proof_id, 'manifest_id': manifest_id, 'part_index': part_index,
            'domain': domain, 'sha': digest}


def _restore_xor(manifest, usable):
    for left, right in ((0, 1), (0, 2), (1, 2)):
        pair = next(((a, b) for a in usable.get(left, []) for b in usable.get(right, [])
                     if a['domain'] != b['domain']), None)
        if not pair:
            continue
        parts = {left: pair[0]['raw'], right: pair[1]['raw']}
        if len(parts[left]) != len(parts[right]):
            return 'corrupt', 'shard lengths differ', (left, right), None
        if (left, right) == (0, 1):
            reconstructed = parts[0] + parts[1]
        elif (left, right) == (0, 2):
            reconstructed = parts[0] + _pairwise_xor(parts[0], parts[2])
        else:
            reconstructed = _pairwise_xor(parts[1], parts[2]) + parts[1]
        reconstructed = reconstructed[:manifest['object_size']]
        if _sha(reconstructed) == manifest['object_sha'] == manifest['candidate_digest']:
            return ('restorable', 'distinct shard indexes and domains reconstructed exact object',
                    (left, right), reconstructed)
        return ('corrupt', 'reconstructed object hash differs from manifest or candidate',
                (left, right), reconstructed)
    return 'insufficient', 'no two distinct shard indexes on independent surviving domains', None, None


def _restore_mds(manifest, k, n, usable):
    matrix = _manifest_generator(manifest)
    if len(matrix) != n or any(len(row) != k for row in matrix):
        return 'corrupt', 'manifest encoding matrix does not match its k and n', None, None
    matched = _match_domains(usable)
    if len(matched) < k:
        return ('insufficient',
                f'fewer than {k} distinct shard indexes survive on independent domains',
                None, None)
    chosen = _independent_rows(matrix, sorted(matched), k)
    if chosen is None:
        return ('insufficient',
                f'no invertible {k}-shard combination in the surviving encoding matrix',
                None, None)
    inverse = _invert([matrix[index] for index in chosen])
    if inverse is None:  # pragma: no cover - _independent_rows already established rank k
        return 'insufficient', f'no invertible {k}-shard combination', None, None
    shards = [matched[index]['raw'] for index in chosen]
    width = (manifest['object_size'] + k - 1) // k
    if any(len(shard) != width for shard in shards):
        return 'corrupt', 'shard lengths differ from the encoded object size', chosen, None
    reconstructed = _reconstruct(inverse, shards, width)[:manifest['object_size']]
    digest = _sha(reconstructed)
    if digest == manifest['object_sha'] == manifest['candidate_digest']:
        return ('restorable', f'systematic MDS {k}-of-{n} reconstruction matches manifest and candidate',
                chosen, reconstructed)
    if digest == manifest['object_sha']:
        return ('corrupt', 'reconstructed object matches manifest but not archive candidate digest',
                chosen, reconstructed)
    return 'corrupt', 'reconstructed object hash differs from manifest', chosen, reconstructed


def restore(db_path, manifest_id, lost_domains=(), output=None):
    lost = sorted(set(lost_domains))
    with closing(connect(db_path)) as db, db:
        manifest, current = _current_manifest(db, manifest_id)
        codec_id, k, n = _manifest_codec(manifest)
        expected = _manifest_shards(manifest)
        proofs = db.execute('SELECT * FROM shard_proof WHERE manifest_id=? ORDER BY id', (manifest_id,)).fetchall()
        usable = {}
        conflicts = []
        for proof in proofs:
            if proof['domain'] in lost:
                continue
            index = int(proof['part_index'])
            if (index < 0 or index >= n or proof['sha'] != expected[index]
                    or (proof['revision'] is not None and proof['revision'] != manifest['revision'])):
                conflicts.append(proof['id'])
            else:
                usable.setdefault(index, []).append(proof)
        selected = None
        reconstructed = None
        if not current:
            status, reason = 'stale', 'manifest or retention plan revision changed'
        elif conflicts:
            status, reason = 'disputed', 'conflicting shard bytes from surviving custody domain'
        elif codec_id == XOR_CODEC:
            status, reason, selected, reconstructed = _restore_xor(manifest, usable)
        else:
            status, reason, selected, reconstructed = _restore_mds(manifest, k, n, usable)
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
                               json.dumps(list(selected) if selected is not None else []),
                               _sha(reconstructed) if reconstructed is not None else None,
                               reason, output_path)).lastrowid
    return {'audit_id': audit_id, 'manifest_id': manifest_id, 'codec_id': codec_id, 'k': k, 'n': n,
            'status': status, 'reason': reason, 'lost_domains': lost,
            'part_indexes': list(selected) if selected is not None else [],
            'reconstructed_sha': _sha(reconstructed) if reconstructed is not None else None,
            'output_path': output_path}


def shard_report(db_path):
    with closing(connect(db_path)) as db:
        manifests = []
        encodings = {}
        for row in db.execute('SELECT * FROM shard_manifest ORDER BY id'):
            item = dict(row)
            codec_id, k, n = _manifest_codec(row)
            matrix = _manifest_generator(row)
            digests = _manifest_shards(row)
            item.update({'codec_id': codec_id, 'k': k, 'n': n, 'generator': matrix,
                         'shard_sha': digests,
                         'encoding': {'codec_id': codec_id, 'k': k, 'n': n, 'generator': matrix}})
            manifests.append(item)
            encodings[row['id']] = {'codec_id': codec_id, 'k': k, 'n': n}
        restores = []
        for row in db.execute('SELECT * FROM shard_restore ORDER BY id'):
            item = dict(row)
            for field in ('lost_domains', 'part_indexes'):
                try:
                    item[field] = json.loads(item[field])
                except (TypeError, json.JSONDecodeError):
                    pass
            item.update(encodings.get(row['manifest_id'], {}))
            restores.append(item)
        return {
            'manifests': manifests,
            'proofs': [dict(x) for x in db.execute('SELECT id,manifest_id,revision,part_index,domain,sha FROM shard_proof ORDER BY id')],
            'restores': restores,
        }
