# 键桥迁移台

本地多表旧键到新键的确定性迁移与引用重写工具。`python -m keybridge demo` 从合成父子表生成计划、在 SQLite 沙箱应用并输出 JSON/Markdown。`python -m pytest -q` 运行测试。仅需 Python 3.11+ 标准库；不连接真实数据库。

输入契约声明表、主键、自然键、外键。依赖图拓扑排序；旧 CSV 原始字节、哈希和行号冻结。自然键规范化后派发新键，碰撞与缺父行阻断；引用边保留旧值和新值。目标快照同键同值可复用，异值阻断。计划固定来源与目标摘要，执行前再次核对。应用在单事务中写入，故障回滚该事务；`rollback` 逆序删除本计划插入的目标行。报告含来源、映射、引用边、冲突、步骤。状态：ready→applied/rolled_back，blocked 不能执行。错误码包括 reference_cycle、unknown_reference、natural_collision、orphan_reference、target_conflict、stale_plan。

回滚前重新核对执行步骤、逆操作台账、计划拥有的目标行内容，以及外部目标行是否引用这些行。任一证据已变化时整次回滚拒绝，目标数据保持原样；不会为了恢复旧摘要而删除后来修改的业务数据。相关错误码为 `apply_ledger_changed`、`rollback_ledger_changed`、`rollback_conflict`、`rollback_referenced_by_external_row`。`pytest` 固定从本仓 `src` 导入源码。

来源导入先核对契约声明的主键、自然键和外键列，缺列或空键不会等到计划执行时才失败。已有目标快照同键异值时拒绝导入并保持原行，避免干跑与真实目标之间出现静默覆盖。计划的来源、目标摘要和回滚证据共同决定能否安全落地或撤销。

迁移落地后的目标核对通过 `target-audit` 检查计划拥有的目标行与引用关系，逐项记录缺失、值分歧和断裂引用，形成 complete 或 divergent 结论。计划执行成功仅表示写入事务完成，不代表目标内容后续仍与映射和引用预期相符。

旧键退役通过 `client-contract`、`lookup-observe` 和 `retirement-gate` 决定。关键客户端必须实际观察到映射后的新键及目标载荷哈希，且目标核对仍有效，才能得到 retire_old_keys；否则为 retain_aliases。合同、观测和每次闸门成员证据持久保存。`demo-decisions` 演示完整链路；测试覆盖目标漂移、缺失观测、错误映射与重复序号冲突。

迁移完成后的旧源更正由独立的 `correction-import` → `correction-prepare` → `correction-apply` 流程处理。输入是 JSON 数组，每项提供 `table`、`old_key`、`expected_revision` 和完整的旧源 `after` 行。系统保存原始批次、每行更正版本、目标投影及逐字段冲突。更正外键先经冻结的旧新键映射转换；主键和自然键不允许借更正流程变更。准备时以旧源当前版本、目标现值和更正后投影做三方合并：目标独立改动的字段被保留，同字段竞争进入冲突隔离，其他行仍可落地。应用前再次核对版本和目标内容，避免准备后目标变化造成静默覆盖。`correction-report` 展示批次与冲突，`demo-corrections` 可运行完整样例。已落地的更正会成为后续 `target-audit` 的预期目标；旧审计因目标摘要变化失效，须重新审计并重新取得客户端观测后才可退役旧键。

迁移后主体授权与凭证核验见 [建仓需求补充 v2](docs/BUILD_REQUIREMENTS_V2_AUTHORIZATION.md)。`auth-import` 把旧身份的授权/撤销版本入库，`access-check` 要求唯一键映射、已应用计划、当前完整目标审计和字段/用途授权；`credential-issue` 签发只存摘要的凭证，`credential-verify` 在撤销、目标更正或回滚后重新裁定。`auth-report` 输出证据但不输出 token，`demo-auth` 用合成数据演示允许→撤销的结果变化。真实认证和密钥托管不由本地模拟器承担。

来源归档是独立工作流：`archive-contract`、`archive-candidates` 和 `archive-plan` 在法定保留、数据群覆盖与有限预算之间裁定哪些来源批次应留存；`archive-replica`、`archive-audit` 核实选中批次能否从不同保管域恢复。摘要冲突、域重复和候选版本变化分别产生争议、脆弱和旧计划失效结论，持久化记录可由 `archive-report` 查询。命令和反例见 [建仓需求补充 v3](docs/BUILD_REQUIREMENTS_V3_ARCHIVE.md)。

真实离线分片恢复见 [建仓需求补充 v4](docs/BUILD_REQUIREMENTS_V4_SHARDS.md)：对保留对象的实际字节做 2-of-3 XOR 分片，按独立故障域失去分片的场景重建对象、核对整对象 SHA-256，并记录 manifest 版次、冲突分片及每次恢复审计。完整副本回执与分片字节验证并行保留，二者结论不混用。

迁移前来源质量隔离、独立修复复核与整批放行见 [建仓需求补充 v5](docs/BUILD_REQUIREMENTS_V5_QUALITY.md)。这条链路保存原始来源、逐行隔离原因和修复审批证据；整批质量通过后才导出 CSV 与 manifest 摘要。它独立于迁移后的旧源更正流程。

多源回款对账与结算账本见 [建仓需求补充 v6](docs/BUILD_REQUIREMENTS_V6_SETTLEMENT.md)。`settlement-import` 保存账单、银行回款、分配凭证和迟到冲正，`settlement-evaluate` 重算当前版本，`settlement-report` 查询历史及分配边；`demo-settlement` 展示一笔回款拆给两笔应收、两笔回款共同覆盖同一应收。金额使用整数分；同额多解、跨主体错配和归属不明的拆分冲正进入争议。

客户贷项是另一类经济义务，见 [建仓需求补充 v7](docs/BUILD_REQUIREMENTS_V7_CREDIT.md)：独立审批后先抵扣未付应收，已付部分按签约合同形成退款义务或可用于未来发票的客户余额；应用、履行、过期、撤销和迟到冲正争议均留在贷项账本，不改写旧银行结算。

离线特征训练发布是独立的数据应用流程，见 [建仓需求补充 v8](docs/BUILD_REQUIREMENTS_V8_FEATURES.md)：特征观测的事件时间和可用时间共同决定训练资格；标签、独立在线捕获与发布闸口分别持久化，迟到修订不能倒灌历史预测，在线值或观测版次不符会阻断训练版本发布。
