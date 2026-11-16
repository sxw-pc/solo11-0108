# 键桥迁移台

本地多表旧键到新键的确定性迁移与引用重写工具。`python -m keybridge demo` 从合成父子表生成计划、在 SQLite 沙箱应用并输出 JSON/Markdown。`python -m pytest -q` 运行测试。仅需 Python 3.11+ 标准库；不连接真实数据库。

输入契约声明表、主键、自然键、外键。依赖图拓扑排序；旧 CSV 原始字节、哈希和行号冻结。自然键规范化后派发新键，碰撞与缺父行阻断；引用边保留旧值和新值。目标快照同键同值可复用，异值阻断。计划固定来源与目标摘要，执行前再次核对。应用在单事务中写入，故障回滚该事务；`rollback` 逆序删除本计划插入的目标行。报告含来源、映射、引用边、冲突、步骤。状态：ready→applied/rolled_back，blocked 不能执行。错误码包括 reference_cycle、unknown_reference、natural_collision、orphan_reference、target_conflict、stale_plan。

回滚前重新核对执行步骤、逆操作台账、计划拥有的目标行内容，以及外部目标行是否引用这些行。任一证据已变化时整次回滚拒绝，目标数据保持原样；不会为了恢复旧摘要而删除后来修改的业务数据。相关错误码为 `apply_ledger_changed`、`rollback_ledger_changed`、`rollback_conflict`、`rollback_referenced_by_external_row`。`pytest` 固定从本仓 `src` 导入源码。

来源导入先核对契约声明的主键、自然键和外键列，缺列或空键不会等到计划执行时才失败。已有目标快照同键异值时拒绝导入并保持原行，避免干跑与真实目标之间出现静默覆盖。计划的来源、目标摘要和回滚证据共同决定能否安全落地或撤销。

目标数据摄取使用不可变代际（generation）。每次接受的导入都新增一代，父代是导入开始时活动的代；活动指针与物化 `target_row` 在同一事务内切换，旧代行只增不改，后续导入、计划应用、回滚和更正都不会回写旧代。导入合同必须显式声明：

- `full_snapshot`：本次文件代表其点名表的整套目标；文件中缺失的键在新代退役。未点名的表沿用父代。
- `delta`：文件只表达显式变更；缺失的键保持父代原值。增量必须用 `--base-generation` 声明基代；若其他导入者已推进活动代，旧基代发布会以 `stale_base_generation` 拒绝，不能覆盖新行。

`import_target(..., mode='legacy')` 保留旧行为：缺失键不动、同键异值以 `target_snapshot_conflict` 拒绝。相同文件 SHA 在 `full_snapshot` 与 `delta` 下是不同合同，各自生成新代；完全相同的（表、模式、SHA、基代、契约摘要）导入重放幂等，返回原代。

多表导入使用 `target-generation TABLE=FILE ...`，在提交前检查 `table_contract.refs` 的整体闭包：父键在本代删除而子行仍在时整代拒绝（`reference_closure_violation`），同代一起删除子与父则可原子发布。计划记录目标代际及摘要，应用前若活动代已变化则以 `stale_plan` 拒绝；更正批次、`correction_head`、审计与退役闸门均记录各自的代际和摘要。旧审计/闸门继续指向其代际而不会按新代重算，新代出现后旧闸门自动降级为 `retain_aliases`。

迁移落地后的目标核对通过 `target-audit` 检查计划拥有的目标行与引用关系，逐项记录缺失、值分歧和断裂引用，形成 complete 或 divergent 结论。计划执行成功仅表示写入事务完成，不代表目标内容后续仍与映射和引用预期相符。

旧键退役通过 `client-contract`、`lookup-observe` 和 `retirement-gate` 决定。关键客户端必须实际观察到映射后的新键及目标载荷哈希，且目标核对仍有效，才能得到 retire_old_keys；否则为 retain_aliases。合同、观测和每次闸门成员证据持久保存。`demo-decisions` 演示完整链路；测试覆盖目标漂移、缺失观测、错误映射与重复序号冲突。

迁移完成后的旧源更正由独立的 `correction-import` → `correction-prepare` → `correction-apply` 流程处理。输入是 JSON 数组，每项提供 `table`、`old_key`、`expected_revision` 和完整的旧源 `after` 行。系统保存原始批次、每行更正版本、目标投影及逐字段冲突。更正外键先经冻结的旧新键映射转换；主键和自然键不允许借更正流程变更。准备时以旧源当前版本、目标现值和更正后投影做三方合并：目标独立改动的字段被保留，同字段竞争进入冲突隔离，其他行仍可落地。应用前再次核对版本和目标内容，避免准备后目标变化造成静默覆盖。`correction-report` 展示批次与冲突，`demo-corrections` 可运行完整样例。已落地的更正会成为后续 `target-audit` 的预期目标；旧审计因目标摘要变化失效，须重新审计并重新取得客户端观测后才可退役旧键。
