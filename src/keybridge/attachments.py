"""大型二进制附件的内容寻址（CAS）sidecar 与崩溃安全恢复。

架构路线：外置 CAS sidecar（路线 B）。附件字节不进入 SQLite；target_row
载荷只保存稳定引用 {'$cas': sha256, 'name', 'size'}。相同字节全库只存一份
（内容去重），源文件以 1MB 块流式读写，单文件上限 2GB，处理内存峰值远
低于 32MB。路线 A（字节内嵌 SQLite 与行共享单一事务）无法同时满足去重
与内存上限，且数据库备份会随字节数膨胀，故不采用。

对象发布（文件系统）与行引用提交（SQLite 事务）不共享事务，崩溃安全由
attachment_journal 持久操作日志与 recover 命令保证：

1. 发布协议：源对象逐块读入 cas/tmp/<uuid>.part 并累计 SHA-256，fsync 耐久
   后原子 rename 到 objects/<sha[:2]>/<sha>，再于同一 DB 事务登记
   attachment_object 与 journal。rename 的原子性保证 CAS 位置不出现半文件；
   同名不同内容的源文件落在不同摘要位置，互不覆盖。
2. 引用提交：apply 先在事务外确保全部被引对象已持久发布（缺失时按 manifest
   登记的来源路径重新暂存并复核摘要），再在单个 SQLite 事务内写入
   target_row、attachment_ref（plan 拥有的行引用）、journal ref_commit 与
   apply 状态。已提交引用绝不指向缺失对象；事务失败整体回滚，不留行引用。
3. 恢复语义：recover 清理无引用临时对象（tmp/ 残留）、收养已发布但未登记的
   CAS 孤儿（重算摘要校验，损坏者隔离）、并按引用归零原则回收对象——
   存活集 = 各 plan 的 attachment_ref ∪ 目标行载荷引用 ∪ ready 计划待提交
   意图。rollback 只删除本 plan 的引用（同事务 journal ref_release），最后
   引用释放后的对象由 recover 安全 GC；清理重跑幂等。

备份语义：SQLite 与 CAS 目录需协调备份——对象只增不改且引用先于提交发布，
先备份数据库再备份 CAS 目录；恢复后以 attachment-report --verify 校验摘要、
长度与引用一致性，缺失对象在来源文件仍在时可由 apply 重新暂存。备份窗口
内不要并发运行 recover 的 GC。
"""

import hashlib
import json
import os
import uuid
from contextlib import closing
from pathlib import Path

from .store import connect

CHUNK = 1024 * 1024
MAX_OBJECT_SIZE = 2 * 1024 ** 3
_JOURNAL_COLS = ('plan_id', 'table_name', 'old_key', 'new_key', 'field', 'sha', 'size', 'path', 'detail')


def cas_dir_for(db_path, cas_dir=None):
    return Path(cas_dir) if cas_dir else Path(str(db_path) + '.cas')


def _layout(cas):
    (cas / 'tmp').mkdir(parents=True, exist_ok=True)
    (cas / 'objects').mkdir(parents=True, exist_ok=True)


def _object_path(cas, sha):
    return Path(cas) / 'objects' / sha[:2] / sha


def _fsync_dir(path):
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash_file(path):
    h = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            h.update(chunk)
    return h.hexdigest(), size


def journal(db, op, **kw):
    row = {k: kw.get(k) for k in _JOURNAL_COLS}
    db.execute('INSERT INTO attachment_journal(op,plan_id,table_name,old_key,new_key,field,sha,size,path,detail)'
               ' VALUES (?,?,?,?,?,?,?,?,?,?)',
               (op, row['plan_id'], row['table_name'], row['old_key'], row['new_key'], row['field'],
                row['sha'], row['size'], row['path'], row['detail']))


def _register(db_path, sha, size, origin, reused):
    with closing(connect(db_path)) as db, db:
        db.execute('INSERT OR IGNORE INTO attachment_object(sha,size,status,origin) VALUES (?,?,?,?)',
                   (sha, size, 'published', origin))
        journal(db, 'publish', sha=sha, size=size,
                detail=json.dumps({'origin': origin, 'reused': reused}, sort_keys=True))


def publish_file(db_path, source, cas_dir=None, expected_sha=None, max_size=MAX_OBJECT_SIZE, origin='import'):
    """源对象逐块流式暂存、校验最终摘要后原子发布进 CAS；相同字节只存一份。"""
    cas = cas_dir_for(db_path, cas_dir)
    _layout(cas)
    source = Path(source)
    if not source.is_file():
        raise ValueError('attachment_source_missing')
    if source.stat().st_size > max_size:
        raise ValueError('attachment_too_large')
    tmp = cas / 'tmp' / (uuid.uuid4().hex + '.part')
    h = hashlib.sha256()
    written = 0
    try:
        with source.open('rb') as src, tmp.open('wb') as out:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_size:
                    raise ValueError('attachment_too_large')
                h.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    sha = h.hexdigest()
    if expected_sha and sha != expected_sha:
        tmp.unlink(missing_ok=True)
        raise ValueError('attachment_source_changed')
    final = _object_path(cas, sha)
    reused = False
    if final.exists():
        if final.stat().st_size != written:
            tmp.unlink(missing_ok=True)
            raise ValueError('cas_object_corrupt')
        tmp.unlink()
        reused = True
    else:
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tmp), str(final))
        _fsync_dir(final.parent)
    _register(db_path, sha, written, origin, reused)
    return {'sha': sha, 'size': written, 'reused': reused}


def ensure_published(db_path, sha, size, cas_dir=None):
    """apply 提交行引用前确保对象已持久发布：已在 CAS 则复用，否则从来源重暂存。"""
    cas = cas_dir_for(db_path, cas_dir)
    _layout(cas)
    final = _object_path(cas, sha)
    if final.is_file():
        if final.stat().st_size != size:
            raise ValueError('cas_object_corrupt')
        _register(db_path, sha, size, 'restage', True)
        return {'sha': sha, 'size': size, 'reused': True}
    with closing(connect(db_path)) as db:
        paths = [r['path'] for r in
                 db.execute('SELECT path FROM attachment_manifest WHERE sha=? ORDER BY id', (sha,))]
    if not paths:
        raise ValueError('attachment_object_missing')
    error = None
    for path in paths:
        try:
            return publish_file(db_path, path, cas, expected_sha=sha, origin='restage')
        except ValueError as exc:
            error = exc
    raise error


def import_manifest(db_path, manifest_path, cas_dir=None):
    """导入附件 manifest：登记 (table,old_key,field)→本地文件 映射并立即发布对象。"""
    entries = json.loads(Path(manifest_path).read_text(encoding='utf-8'))
    if not isinstance(entries, list):
        raise ValueError('manifest_invalid')
    seen = set()
    for e in entries:
        if not isinstance(e, dict) or any(not isinstance(e.get(k), str) or not e.get(k)
                                          for k in ('table', 'old_key', 'field', 'path')):
            raise ValueError('manifest_entry_invalid')
        key = (e['table'], e['old_key'], e['field'])
        if key in seen:
            raise ValueError('manifest_entry_duplicate')
        seen.add(key)
    imported = updated = 0
    objects = []
    for e in entries:
        key = (e['table'], e['old_key'], e['field'])
        pub = publish_file(db_path, e['path'], cas_dir)
        with closing(connect(db_path)) as db, db:
            old = db.execute('SELECT * FROM attachment_manifest WHERE table_name=? AND old_key=? AND field=?',
                             key).fetchone()
            if old:
                if old['sha'] != pub['sha']:
                    raise ValueError('attachment_manifest_conflict')
                if old['path'] != e['path']:
                    db.execute('UPDATE attachment_manifest SET path=? WHERE id=?', (e['path'], old['id']))
                    journal(db, 'manifest_update', table_name=key[0], old_key=key[1], field=key[2],
                            sha=pub['sha'], size=pub['size'], path=e['path'])
                    updated += 1
            else:
                db.execute('INSERT INTO attachment_manifest(table_name,old_key,field,path,sha,size)'
                           ' VALUES (?,?,?,?,?,?)', (*key, e['path'], pub['sha'], pub['size']))
                journal(db, 'manifest_register', table_name=key[0], old_key=key[1], field=key[2],
                        sha=pub['sha'], size=pub['size'], path=e['path'])
                imported += 1
        objects.append({'table': key[0], 'old_key': key[1], 'field': key[2],
                        'sha': pub['sha'], 'size': pub['size'], 'reused': pub['reused']})
    return {'imported': imported, 'updated': updated, 'objects': objects}


def extract_refs(payload_text):
    """从行载荷 JSON 中提取全部 {'$cas': sha} 附件引用。"""
    found = set()
    try:
        data = json.loads(payload_text)
    except ValueError:
        return found
    stack = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            cas = node.get('$cas')
            if isinstance(cas, str):
                found.add(cas)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return found


def live_shas(db):
    """存活引用集：各 plan 已提交引用 ∪ 目标行载荷引用 ∪ ready 计划待提交意图。"""
    live = {r['sha'] for r in db.execute('SELECT DISTINCT sha FROM attachment_ref')}
    for row in db.execute('SELECT payload FROM target_row'):
        live.update(extract_refs(row['payload']))
    for row in db.execute("SELECT actions FROM migration_plan WHERE status='ready'"):
        for action in json.loads(row['actions']):
            for att in action.get('attachments', []):
                live.add(att['sha'])
    return live


def recover(db_path, cas_dir=None):
    """崩溃恢复与幂等清理：无引用临时对象、CAS 孤儿收养、引用归零对象 GC。"""
    cas = cas_dir_for(db_path, cas_dir)
    _layout(cas)
    tmp_removed = []
    for f in sorted((cas / 'tmp').iterdir()):
        if f.is_file():
            f.unlink()
            tmp_removed.append(f.name)
    adopted = []
    quarantined = []
    with closing(connect(db_path)) as db:
        known = {r['sha'] for r in db.execute('SELECT sha FROM attachment_object')}
    for f in sorted((cas / 'objects').rglob('*')):
        if not f.is_file():
            continue
        sha = f.name
        valid = (len(sha) == 64 and all(c in '0123456789abcdef' for c in sha)
                 and f.parent.name == sha[:2] and f.parent.parent == cas / 'objects')
        if valid and sha in known:
            continue
        if valid:
            digest, size = _hash_file(f)
            if digest == sha:
                with closing(connect(db_path)) as db, db:
                    db.execute('INSERT OR IGNORE INTO attachment_object(sha,size,status,origin) VALUES (?,?,?,?)',
                               (sha, size, 'published', 'adopted'))
                    journal(db, 'adopt', sha=sha, size=size, path=str(f))
                adopted.append(sha)
                continue
        quarantine = cas / 'quarantine'
        quarantine.mkdir(exist_ok=True)
        dest = quarantine / f.name
        if dest.exists():
            dest = quarantine / (f.name + '.' + uuid.uuid4().hex)
        os.replace(str(f), str(dest))
        with closing(connect(db_path)) as db, db:
            journal(db, 'quarantine', sha=sha if valid else None, path=str(dest))
        quarantined.append(f.name)
    collected = []
    with closing(connect(db_path)) as db:
        live = live_shas(db)
        rows = db.execute('SELECT sha,size FROM attachment_object ORDER BY sha').fetchall()
    for row in rows:
        if row['sha'] in live:
            continue
        _object_path(cas, row['sha']).unlink(missing_ok=True)
        with closing(connect(db_path)) as db, db:
            db.execute('DELETE FROM attachment_object WHERE sha=?', (row['sha'],))
            journal(db, 'gc', sha=row['sha'], size=row['size'])
        collected.append({'sha': row['sha'], 'size': row['size']})
    summary = {'tmp_removed': len(tmp_removed), 'adopted': adopted,
               'quarantined': quarantined, 'collected': collected}
    with closing(connect(db_path)) as db, db:
        journal(db, 'recover', detail=json.dumps(summary, sort_keys=True))
    return summary


def attachment_report(db_path, cas_dir=None, verify=False):
    """审计对象摘要、长度与引用一致性；verify=True 时流式重算对象摘要。"""
    cas = cas_dir_for(db_path, cas_dir)
    objects = []
    dangling = []
    with closing(connect(db_path)) as db:
        for row in db.execute('SELECT * FROM attachment_object ORDER BY sha'):
            f = _object_path(cas, row['sha'])
            status = 'ok'
            if not f.is_file():
                status = 'missing_file'
            elif f.stat().st_size != row['size']:
                status = 'size_mismatch'
            elif verify and _hash_file(f)[0] != row['sha']:
                status = 'digest_mismatch'
            refs = db.execute('SELECT COUNT(*) FROM attachment_ref WHERE sha=?', (row['sha'],)).fetchone()[0]
            objects.append({'sha': row['sha'], 'size': row['size'], 'status': status, 'plan_refs': refs})
        for row in db.execute('SELECT DISTINCT sha FROM attachment_ref'):
            if not _object_path(cas, row['sha']).is_file():
                dangling.append({'kind': 'plan_ref', 'sha': row['sha']})
        for row in db.execute('SELECT table_name,new_key,payload FROM target_row'):
            for sha in extract_refs(row['payload']):
                if not _object_path(cas, sha).is_file():
                    dangling.append({'kind': 'target_row', 'table': row['table_name'],
                                     'new_key': row['new_key'], 'sha': sha})
        manifests = [dict(x) for x in db.execute('SELECT * FROM attachment_manifest ORDER BY id')]
    missing_sources = [{'table': m['table_name'], 'old_key': m['old_key'], 'field': m['field'], 'path': m['path']}
                       for m in manifests if not Path(m['path']).is_file()]
    bad = [o for o in objects if o['status'] != 'ok']
    return {'objects': objects, 'dangling_refs': dangling, 'manifest_entries': len(manifests),
            'missing_sources': missing_sources,
            'status': 'ok' if not bad and not dangling else 'divergent'}
