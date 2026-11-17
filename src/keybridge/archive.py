"""Independent source-archive retention and recoverability workflow."""

import hashlib
import itertools
import json
from contextlib import closing
from pathlib import Path

from .store import connect


def _load(path):
    raw = Path(path).read_bytes()
    return json.loads(raw), raw, hashlib.sha256(raw).hexdigest()


def register_contract(db_path, path):
    item, _, _ = _load(path)
    ident = str(item['id'])
    budget, minimum, quorum = (int(item[key]) for key in ('budget', 'min_per_cohort', 'replica_quorum'))
    if not ident or budget < 1 or minimum < 1 or quorum < 2:
        raise ValueError('invalid archive contract')
    with closing(connect(db_path)) as db, db:
        old = db.execute('SELECT * FROM archive_contract WHERE id=?', (ident,)).fetchone()
        if old:
            if (old['budget'], old['min_per_cohort'], old['replica_quorum']) != (budget, minimum, quorum):
                raise ValueError('contract is immutable; use a new contract id')
            return {'contract': ident, 'revision': old['revision'], 'idempotent': True}
        db.execute('INSERT INTO archive_contract(id,budget,min_per_cohort,replica_quorum) VALUES(?,?,?,?)',
                   (ident, budget, minimum, quorum))
    return {'contract': ident, 'revision': 0}


def import_candidates(db_path, path):
    items, raw, sha = _load(path)
    if not isinstance(items, list) or not items:
        raise ValueError('candidate input must be a nonempty JSON list')
    with closing(connect(db_path)) as db, db:
        changed = set()
        for item in items:
            ident, contract, cohort = (str(item[key]) for key in ('id', 'contract', 'cohort'))
            units, value, hold = int(item['units']), int(item['value']), bool(item['legal_hold'])
            digest = str(item['digest'])
            if not all((ident, contract, cohort, digest)) or units < 1 or value < 0:
                raise ValueError('invalid archive candidate')
            if not db.execute('SELECT 1 FROM archive_contract WHERE id=?', (contract,)).fetchone():
                raise ValueError('unknown archive contract')
            old = db.execute('SELECT * FROM archive_candidate WHERE id=?', (ident,)).fetchone()
            if old:
                if (old['contract_id'], old['cohort'], old['units'], old['value'], old['digest'], old['legal_hold']) != (
                        contract, cohort, units, value, digest, int(hold)):
                    raise ValueError('candidate id reused with changed terms')
                continue
            db.execute('INSERT INTO archive_candidate VALUES(?,?,?,?,?,?,?,?,?)',
                       (ident, contract, cohort, units, value, digest, int(hold), sha, raw))
            changed.add(contract)
        for contract in changed:
            db.execute('UPDATE archive_contract SET revision=revision+1 WHERE id=?', (contract,))
        return {'imported': len(items), 'changed_contracts': sorted(changed)}


def _selection(rows, budget, minimum):
    if not rows:
        return None, 0, 0
    if len(rows) > 18:
        raise ValueError('at most 18 candidate lots per contract for exact planning')
    cohorts = {r['cohort'] for r in rows}
    optional = [r for r in rows if not r['legal_hold']]
    best = None
    for take in range(len(optional) + 1):
        for subset in itertools.combinations(optional, take):
            selected = [r for r in rows if r['legal_hold']] + list(subset)
            used = sum(r['units'] for r in selected)
            if used > budget:
                continue
            if any(sum(r['cohort'] == cohort for r in selected) < minimum for cohort in cohorts):
                continue
            value = sum(r['value'] for r in selected)
            ids = tuple(sorted(r['id'] for r in selected))
            rank = (value, -used)
            if best is None or rank > best[0] or (rank == best[0] and ids < best[1]):
                best = (rank, ids, used, value)
    return (best[1], best[2], best[3]) if best else (None, 0, 0)


def plan_archive(db_path, contract_id):
    with closing(connect(db_path)) as db, db:
        contract = db.execute('SELECT * FROM archive_contract WHERE id=?', (contract_id,)).fetchone()
        if not contract:
            raise ValueError('unknown archive contract')
        rows = db.execute('SELECT * FROM archive_candidate WHERE contract_id=? ORDER BY id', (contract_id,)).fetchall()
        selected, used, value = _selection(rows, contract['budget'], contract['min_per_cohort'])
        status = 'ready' if selected is not None else 'blocked'
        reason = ('maximum value under legal holds, cohort coverage and storage budget'
                  if selected is not None else 'legal holds and cohort coverage exceed storage budget')
        cursor = db.execute('INSERT INTO archive_plan(contract_id,revision,status,used_units,total_value,reason) '
                            'VALUES(?,?,?,?,?,?)', (contract_id, contract['revision'], status, used, value, reason))
        plan_id = cursor.lastrowid
        for row in rows:
            decision = ('retain' if row['id'] in selected else 'defer') if selected is not None else 'blocked'
            item_reason = ('legal hold' if row['legal_hold'] and decision == 'retain' else
                           'selected under cohort and value constraints' if decision == 'retain' else
                           'budget allocation' if decision == 'defer' else reason)
            db.execute('INSERT INTO archive_plan_item VALUES(?,?,?,?)',
                       (plan_id, row['id'], decision, item_reason))
        return {'plan_id': plan_id, 'revision': contract['revision'], 'status': status,
                'selected': list(selected) if selected is not None else [], 'used_units': used,
                'total_value': value, 'reason': reason}


def import_replica(db_path, path):
    item, raw, sha = _load(path)
    ident, candidate, domain, digest = (str(item[key]) for key in ('proof_id', 'candidate', 'domain', 'digest'))
    plan_id = int(item['plan_id'])
    if not all((ident, candidate, domain, digest)):
        raise ValueError('invalid replica proof')
    with closing(connect(db_path)) as db, db:
        plan = db.execute('SELECT p.*,c.revision AS current_revision FROM archive_plan p '
                          'JOIN archive_contract c ON c.id=p.contract_id WHERE p.id=?', (plan_id,)).fetchone()
        if not plan or plan['status'] != 'ready' or plan['revision'] != plan['current_revision']:
            raise ValueError('plan missing, blocked or stale')
        selected = db.execute("SELECT 1 FROM archive_plan_item WHERE plan_id=? AND candidate_id=? AND decision='retain'",
                              (plan_id, candidate)).fetchone()
        if not selected:
            raise ValueError('replica is not in retained plan')
        old = db.execute('SELECT source_sha FROM archive_replica_proof WHERE id=?', (ident,)).fetchone()
        if old:
            if old['source_sha'] != sha:
                raise ValueError('proof id reused with different bytes')
            return {'proof_id': ident, 'idempotent': True}
        db.execute('INSERT INTO archive_replica_proof VALUES(?,?,?,?,?,?,?)',
                   (ident, plan_id, candidate, domain, digest, sha, raw))
    return {'proof_id': ident, 'candidate': candidate, 'domain': domain}


def audit_archive(db_path, plan_id):
    with closing(connect(db_path)) as db, db:
        plan = db.execute('SELECT p.*,c.revision AS current_revision,c.replica_quorum '
                          'FROM archive_plan p JOIN archive_contract c ON c.id=p.contract_id WHERE p.id=?',
                          (plan_id,)).fetchone()
        if not plan:
            raise ValueError('unknown archive plan')
        if plan['status'] != 'ready' or plan['revision'] != plan['current_revision']:
            overall, reason = 'stale', 'plan cannot certify current candidate revision'
        else:
            overall, reason = 'recoverable', 'all selected lots have independent matching replicas'
        cursor = db.execute('INSERT INTO archive_audit(plan_id,status,reason) VALUES(?,?,?)',
                            (plan_id, overall, reason))
        audit_id = cursor.lastrowid
        results = []
        for row in db.execute("SELECT c.id,c.digest FROM archive_plan_item i JOIN archive_candidate c ON c.id=i.candidate_id "
                              "WHERE i.plan_id=? AND i.decision='retain' ORDER BY c.id", (plan_id,)).fetchall():
            proofs = db.execute('SELECT domain,digest FROM archive_replica_proof WHERE plan_id=? AND candidate_id=?',
                                (plan_id, row['id'])).fetchall()
            matching = sorted({p['domain'] for p in proofs if p['digest'] == row['digest']})
            conflicts = sorted({p['domain'] for p in proofs if p['digest'] != row['digest']})
            if conflicts:
                status, item_reason = 'disputed', 'custodian digest conflicts with selected source'
            elif len(matching) >= plan['replica_quorum']:
                status, item_reason = 'recoverable', 'independent custody domains meet quorum'
            elif matching:
                status, item_reason = 'fragile', 'too few independent custody domains'
            else:
                status, item_reason = 'unrecoverable', 'no matching replica evidence'
            if overall == 'recoverable' and status != 'recoverable':
                overall, reason = 'at_risk', 'one or more retained lots cannot be independently restored'
            db.execute('INSERT INTO archive_audit_item VALUES(?,?,?,?,?)',
                       (audit_id, row['id'], status, json.dumps({'matching': matching, 'conflicts': conflicts}), item_reason))
            results.append({'candidate': row['id'], 'status': status, 'matching_domains': matching,
                            'conflict_domains': conflicts})
        db.execute('UPDATE archive_audit SET status=?,reason=? WHERE id=?', (overall, reason, audit_id))
    return {'audit_id': audit_id, 'plan_id': plan_id, 'status': overall, 'items': results}


def archive_report(db_path):
    with closing(connect(db_path)) as db:
        return {name: [dict(row) for row in db.execute(query)] for name, query in {
            'contracts': 'SELECT * FROM archive_contract ORDER BY id',
            'candidates': 'SELECT id,contract_id,cohort,units,value,digest,legal_hold,source_sha FROM archive_candidate ORDER BY id',
            'plans': 'SELECT * FROM archive_plan ORDER BY id',
            'plan_items': 'SELECT * FROM archive_plan_item ORDER BY plan_id,candidate_id',
            'replicas': 'SELECT id,plan_id,candidate_id,domain,digest,source_sha FROM archive_replica_proof ORDER BY id',
            'audits': 'SELECT * FROM archive_audit ORDER BY id',
            'audit_items': 'SELECT * FROM archive_audit_item ORDER BY audit_id,candidate_id',
        }.items()}
