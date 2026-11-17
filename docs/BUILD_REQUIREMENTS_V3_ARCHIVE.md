# 建仓需求补充 v3：来源归档取舍与可恢复证明

本增量在键迁移与访问授权之外增加独立的来源归档工作流。原始建仓提示词及已有版本化需求补充保持历史不改；归档只处理合成来源批次的保留和异地副本证据，不读取真实数据库或密钥。

## 两个独立业务决定

**保留预算分配。** 归档合同冻结单位预算、每个数据群的最低保留数量和副本门槛。候选批次记录数据群、存储单位、审计价值、摘要及法定保留标志；原始 JSON 字节与来源哈希不可覆盖。计划在不超过预算的前提下强制包含法定保留批次并覆盖每个数据群，再选择最大审计价值，平分时优先较少资源。计划逐批记录 `retain`、`defer` 或 `blocked` 原因。若法定保留与覆盖要求合计无法满足预算，整份计划 `blocked`，不能通过牺牲受保留批次伪造可行。候选集版本变化使旧计划失效。

**独立副本可恢复核验。** 对已选批次采录保管域、摘要与证明原文。两个证明来自同一保管域只算一份；摘要与所选批次冲突的保管域使该批次 `disputed`，不能靠另两份匹配证明掩盖。匹配域不足门槛为 `fragile`，完全无匹配证明为 `unrecoverable`。核验逐批保留状态与域证据，整体仅在全部选中批次都满足独立副本门槛时为 `recoverable`，否则 `at_risk`；候选集修订后旧计划核验为 `stale`。这决定能否放心删除未选批次，不等同于“计划已写入”。

两个决定的因果链是候选集/预算/法定保留 → 计划选取 → 保管域证明 → 逐批可恢复结论与整体审计。归档结果不改 `migration_plan`、`target_row`、`access_grant` 或凭证验证；因此是独立数据工程工作流，不是键迁移或授权阶段。

## 离线运行与反例

在仓库根目录安装本项目后运行：

```powershell
python -m keybridge archive-contract tests/fixtures/archive_contract.json --db archive.sqlite
python -m keybridge archive-candidates tests/fixtures/archive_candidates.json --db archive.sqlite
python -m keybridge archive-plan --db archive.sqlite --contract archive-2026q4
python -m keybridge archive-replica tests/fixtures/archive_replica_good.json --db archive.sqlite
python -m keybridge archive-audit --db archive.sqlite --plan 1
python -m keybridge archive-replica tests/fixtures/archive_replica_conflict.json --db archive.sqlite
python -m keybridge archive-audit --db archive.sqlite --plan 1
python -m keybridge archive-report --db archive.sqlite
python -m pytest -q
```

样例预算为 6 单位：A 有法定保留，南北两数据群都须覆盖；最佳计划选择 A、B、C，D 暂缓。同一保管域的重复证明仍不足两域门槛；第二保管域摘要冲突使 A 为 `disputed`。测试还构造全部选中批次两域匹配、预算不可能、候选变更后旧计划失效的反例。每次结果可从 SQLite 中的合同、候选原文、计划成员、证明和审计成员重查。

当前精确规划上限为每合同 18 个候选批次；超过上限明确拒绝而非静默近似。生产级海量归档优化、真实副本读取和密码学保管域认证不在此本地模拟器范围内。
