"""大型附件迁移：外置 CAS sidecar、崩溃安全 journal 与 recover 的固定场景。"""

import hashlib
import json
import tracemalloc

import pytest

from keybridge.attachments import (MAX_OBJECT_SIZE, attachment_report, cas_dir_for, extract_refs,
                                   import_manifest, publish_file, recover)
from keybridge.contract import compile_contract
from keybridge.ingest import import_rows
from keybridge.rules import apply, plan, rollback
from keybridge.store import connect


def write_blob(path, size, seed):
    """确定性生成 size 字节的伪随机内容，相同 seed 得到相同字节。"""
    block = hashlib.sha256(seed).digest() * 4096
    with path.open('wb') as f:
        remaining = size
        while remaining > 0:
            chunk = block if remaining >= len(block) else block[:remaining]
            f.write(chunk)
            remaining -= len(chunk)
    return path


def sha_of(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_env(tmp_path, rows=(('r1', 'A'), ('r2', 'B'))):
    db = tmp_path / 'x.db'
    contract = tmp_path / 'contract.json'
    contract.write_text(json.dumps({'tables': [{'name': 'doc', 'pk': 'id', 'natural': 'code'}]}), encoding='utf-8')
    compile_contract(contract, db)
    csv = tmp_path / 'doc.csv'
    csv.write_text('id,code\n' + ''.join(f'{k},{c}\n' for k, c in rows), encoding='utf-8')
    import_rows(db, 'doc', csv)
    return db


def make_manifest(tmp_path, entries, name='manifest.json'):
    p = tmp_path / name
    p.write_text(json.dumps([{'table': 'doc', 'field': 'attachment', **e} for e in entries]), encoding='utf-8')
    return p


def cas_files(db):
    return [p for p in (cas_dir_for(db) / 'objects').rglob('*') if p.is_file()]


def cas_object(db, sha):
    return cas_dir_for(db) / 'objects' / sha[:2] / sha


def count(db, sql, *args):
    return connect(db).execute(sql, args).fetchone()[0]


def test_shared_20mb_object_single_copy_and_failure_recovery(tmp_path):
    """H3 固定场景：R1/R2 引用完全相同的 20MB 附件，apply 在第二行后注入失败。"""
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'contract.pdf', 20 * 1024 * 1024, b'h3-shared')
    sha = sha_of(att)
    result = import_manifest(db, make_manifest(tmp_path, [
        {'old_key': 'r1', 'path': str(att)},
        {'old_key': 'r2', 'path': str(att)}]))
    assert result['imported'] == 2
    assert [o['reused'] for o in result['objects']] == [False, True]  # 第二份内容去重
    assert [p.name for p in cas_files(db)] == [sha]                   # 相同字节只存一份
    pid, status, actions = plan(db)
    assert status == 'ready'
    for a in actions:
        ref = a['payload']['attachment']
        assert ref['$cas'] == sha and ref['name'] == 'contract.pdf' and ref['size'] == 20 * 1024 * 1024
        assert isinstance(a['payload']['attachment'], dict)           # 稳定引用，不内嵌二进制
    assert plan(db)[2] == actions                                     # 计划确定性
    with pytest.raises(RuntimeError, match='injected_failure'):
        apply(db, pid, fail_after=2)                                  # 第二行后注入失败
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 0          # DB 事务完整回滚
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 0
    assert cas_object(db, sha).exists()                               # 已发布对象保留
    summary = recover(db)
    assert summary['collected'] == []                                 # ready 计划仍引用 → 不回收
    assert cas_object(db, sha).exists()
    mtime = cas_object(db, sha).stat().st_mtime_ns
    apply(db, pid)                                                    # 恢复后重试：复用已发布对象
    assert cas_object(db, sha).stat().st_mtime_ns == mtime            # 未重新拷贝
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 2
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 2
    assert len(cas_files(db)) == 1                                    # 两行共享一个 CAS 对象
    report = attachment_report(db)
    assert report['status'] == 'ok' and report['dangling_refs'] == []


def test_different_bytes_different_objects_same_name_no_overwrite(tmp_path):
    db = make_env(tmp_path)
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    f1 = write_blob(tmp_path / 'a' / 'same-name.pdf', 1024 * 1024, b'one')
    f2 = write_blob(tmp_path / 'b' / 'same-name.pdf', 1024 * 1024, b'two')  # 同名不同内容
    import_manifest(db, make_manifest(tmp_path, [
        {'old_key': 'r1', 'path': str(f1)},
        {'old_key': 'r2', 'path': str(f2)}]))
    assert {p.name for p in cas_files(db)} == {sha_of(f1), sha_of(f2)}      # 不同字节不同对象，互不覆盖


def test_same_hash_different_paths_share_object_keep_row_field_links(tmp_path):
    db = make_env(tmp_path)
    (tmp_path / 'm1').mkdir()
    (tmp_path / 'm2').mkdir()
    f1 = write_blob(tmp_path / 'm1' / 'x.pdf', 2 * 1024 * 1024, b'shared')
    f2 = write_blob(tmp_path / 'm2' / 'y.pdf', 2 * 1024 * 1024, b'shared')  # 不同路径相同字节
    sha = sha_of(f1)
    import_manifest(db, make_manifest(tmp_path, [
        {'old_key': 'r1', 'path': str(f1)},
        {'old_key': 'r2', 'path': str(f2)}]))
    pid, status, actions = plan(db)
    apply(db, pid)
    refs = connect(db).execute('SELECT new_key,field,sha FROM attachment_ref ORDER BY new_key').fetchall()
    assert len(refs) == 2 and {r['sha'] for r in refs} == {sha}
    assert {r['new_key'] for r in refs} == {a['new'] for a in actions}      # 各自行字段关联保留
    assert len(cas_files(db)) == 1                                          # 内容只存一份


def test_crash_before_db_commit_leaves_no_dangling_ref(tmp_path):
    """对象预备后、DB 提交前进程终止：恢复后无已提交悬空引用，重试复用对象。"""
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 3 * 1024 * 1024, b'pre-commit')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    pid, _, _ = plan(db)
    with pytest.raises(RuntimeError, match='injected_failure'):
        apply(db, pid, fail_at='pre_commit')
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 0
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 0
    assert cas_object(db, sha).exists()
    assert recover(db)['collected'] == []
    apply(db, pid)
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 2
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 1
    assert attachment_report(db)['dangling_refs'] == []


def test_rollback_releases_plan_refs_then_recover_gc(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 4 * 1024 * 1024, b'rollback')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [
        {'old_key': 'r1', 'path': str(att)},
        {'old_key': 'r2', 'path': str(att)}]))
    pid, _, _ = plan(db)
    apply(db, pid)
    rollback(db, pid)
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 0
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 0
    assert cas_object(db, sha).exists()                    # rollback 不删文件（DB 事务无法撤销文件删除）
    summary = recover(db)
    assert [c['sha'] for c in summary['collected']] == [sha]  # 引用归零 → 安全 GC
    assert not cas_object(db, sha).exists()
    assert recover(db)['collected'] == []                   # 清理重跑幂等


def test_rollback_interrupted_keeps_refs_and_object(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 2 * 1024 * 1024, b'rollback-crash')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [
        {'old_key': 'r1', 'path': str(att)},
        {'old_key': 'r2', 'path': str(att)}]))
    pid, _, _ = plan(db)
    apply(db, pid)
    with pytest.raises(RuntimeError, match='injected_failure'):
        rollback(db, pid, fail_after=1)                       # rollback 期间终止
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 2  # 事务整体回滚，引用完好
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 2
    assert connect(db).execute('SELECT status FROM migration_plan WHERE id=?', (pid,)).fetchone()[0] == 'applied'
    assert recover(db)['collected'] == []                     # 引用仍存活 → 不回收
    rollback(db, pid)
    assert [c['sha'] for c in recover(db)['collected']] == [sha]


def test_shared_hash_across_plans_not_deleted_until_last_ref(tmp_path):
    db = make_env(tmp_path, rows=(('r1', 'A'),))
    att = write_blob(tmp_path / 'a.bin', 2 * 1024 * 1024, b'cross-plan')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    pid1, _, _ = plan(db)
    apply(db, pid1)
    csv2 = tmp_path / 'doc2.csv'
    csv2.write_text('id,code\nr1,A\nr2,B\n', encoding='utf-8')
    import_rows(db, 'doc', csv2)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r2', 'path': str(att)}], name='m2.json'))
    pid2, status, actions2 = plan(db)
    assert status == 'ready'
    assert {a['old']: a['kind'] for a in actions2} == {'r1': 'reuse', 'r2': 'insert'}
    apply(db, pid2)
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref') == 2
    rollback(db, pid2)                                        # 只释放 pid2 创建的引用
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref WHERE plan_id=?', pid2) == 0
    assert count(db, 'SELECT COUNT(*) FROM attachment_ref WHERE plan_id=?', pid1) == 1
    assert recover(db)['collected'] == []                     # 其它 plan 仍引用 → 不误删
    assert cas_object(db, sha).exists()
    rollback(db, pid1)
    assert [c['sha'] for c in recover(db)['collected']] == [sha]  # 最后引用释放 → GC
    assert not cas_object(db, sha).exists()


def test_streaming_stage_memory_bound(tmp_path):
    """40MB 对象流式暂存，Python 侧分配峰值远低于 32MB 上限。"""
    db = make_env(tmp_path)
    big = write_blob(tmp_path / 'big.bin', 40 * 1024 * 1024, b'memory')
    tracemalloc.start()
    try:
        pub = publish_file(db, big)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 32 * 1024 * 1024
    assert pub['size'] == 40 * 1024 * 1024
    assert cas_object(db, pub['sha']).exists()


def test_object_size_limit(tmp_path):
    db = make_env(tmp_path)
    small = write_blob(tmp_path / 'small.bin', 3 * 1024 * 1024, b'limit')
    with pytest.raises(ValueError, match='attachment_too_large'):
        publish_file(db, small, max_size=2 * 1024 * 1024)
    sparse = tmp_path / 'sparse.bin'
    with sparse.open('wb') as f:
        f.seek(MAX_OBJECT_SIZE)
        f.write(b'x')
    with pytest.raises(ValueError, match='attachment_too_large'):
        publish_file(db, sparse)                              # 2GB+1 在 stat 预检即拒绝
    assert cas_files(db) == []                                # 超限对象不留任何 CAS 文件


def test_attachment_report_detects_tamper_and_missing(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 1024 * 1024, b'audit')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    pid, _, _ = plan(db)
    apply(db, pid)
    assert attachment_report(db, verify=True)['status'] == 'ok'
    obj = cas_object(db, sha)
    data = bytearray(obj.read_bytes())
    data[0] ^= 0xFF
    obj.write_bytes(bytes(data))                              # 篡改对象字节
    report = attachment_report(db, verify=True)
    assert report['objects'][0]['status'] == 'digest_mismatch'
    assert report['status'] == 'divergent'
    obj.unlink()                                              # 对象丢失
    report = attachment_report(db)
    assert report['objects'][0]['status'] == 'missing_file'
    assert report['dangling_refs']                            # 引用缺失对象被审计发现
    assert report['status'] == 'divergent'


def test_manifest_reimport_idempotent_and_conflict(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 1024 * 1024, b'v1')
    m = make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}])
    assert import_manifest(db, m)['imported'] == 1
    again = import_manifest(db, m)
    assert again['imported'] == 0 and again['objects'][0]['reused'] is True
    assert len(cas_files(db)) == 1
    att.write_bytes(b'changed-content')                       # 同键不同内容
    with pytest.raises(ValueError, match='attachment_manifest_conflict'):
        import_manifest(db, m)


def test_manifest_added_after_plan_makes_plan_stale(tmp_path):
    db = make_env(tmp_path)
    pid, status, _ = plan(db)
    assert status == 'ready'
    att = write_blob(tmp_path / 'late.bin', 1024 * 1024, b'late')
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    with pytest.raises(ValueError, match='stale_plan'):
        apply(db, pid)


def test_apply_refuses_missing_object_and_restages_from_source(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 1024 * 1024, b'gone')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    pid, _, _ = plan(db)
    cas_object(db, sha).unlink()                              # CAS 对象丢失
    apply(db, pid)                                            # 来源仍在 → 重新暂存后提交
    assert cas_object(db, sha).exists()
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 2
    rollback(db, pid)
    recover(db)
    cas_object(db, sha).unlink(missing_ok=True)
    att.unlink()                                              # 对象与来源都不可用
    pid2, _, _ = plan(db)
    with pytest.raises(ValueError, match='attachment_source_missing'):
        apply(db, pid2)
    assert count(db, 'SELECT COUNT(*) FROM target_row') == 0  # 绝不出现引用缺失对象的已应用行


def test_recover_adopts_orphan_and_quarantines_corrupt(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 1024 * 1024, b'orphan')
    sha = sha_of(att)
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    plan(db)                                                  # ready 计划引用 sha
    con = connect(db)
    with con:
        con.execute('DELETE FROM attachment_object')          # 模拟 rename 后、登记前崩溃
    junk = cas_dir_for(db) / 'tmp' / 'junk.part'
    junk.write_bytes(b'partial')                              # 无引用临时对象
    bad = cas_dir_for(db) / 'objects' / 'ff' / ('f' * 64)
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b'not-the-hash')                          # 名不副实的损坏对象
    summary = recover(db)
    assert summary['tmp_removed'] == 1
    assert summary['adopted'] == [sha]                        # 已发布共享对象收养
    assert summary['quarantined'] == ['f' * 64]
    assert summary['collected'] == []                         # ready 计划引用 → 保留
    assert connect(db).execute('SELECT origin FROM attachment_object WHERE sha=?', (sha,)).fetchone()[0] == 'adopted'
    assert not bad.exists() and not junk.exists()
    second = recover(db)                                      # 重复 recover 幂等
    assert second == {'tmp_removed': 0, 'adopted': [], 'quarantined': [], 'collected': []}


def test_recover_collects_unreferenced_orphan(tmp_path):
    db = make_env(tmp_path)
    payload = b'orphan-bytes'
    sha = hashlib.sha256(payload).hexdigest()
    dest = cas_object(db, sha)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)                                 # 无登记、无引用的已发布对象
    summary = recover(db)
    assert summary['adopted'] == [sha]
    assert [c['sha'] for c in summary['collected']] == [sha]
    assert not dest.exists()


def test_journal_records_full_lifecycle(tmp_path):
    db = make_env(tmp_path)
    att = write_blob(tmp_path / 'a.bin', 1024 * 1024, b'journal')
    import_manifest(db, make_manifest(tmp_path, [{'old_key': 'r1', 'path': str(att)}]))
    pid, _, _ = plan(db)
    apply(db, pid)
    rollback(db, pid)
    recover(db)
    ops = [r['op'] for r in connect(db).execute('SELECT op FROM attachment_journal ORDER BY id')]
    for expected in ('manifest_register', 'publish', 'ref_commit', 'ref_release', 'gc', 'recover'):
        assert expected in ops


def test_extract_refs_walks_nested_payloads():
    payload = json.dumps({'a': {'$cas': 'x1'}, 'b': [{'$cas': 'x2'}, {'c': 'v'}], 'd': 'e'})
    assert extract_refs(payload) == {'x1', 'x2'}
    assert extract_refs('not json') == set()
