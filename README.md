# 键桥迁移台

本地多表旧键到新键的确定性迁移与引用重写工具。`python -m keybridge demo` 从合成父子表生成计划、在 SQLite 沙箱应用并输出 JSON/Markdown。`python -m pytest -q` 运行测试。仅需 Python 3.11+ 标准库；不连接真实数据库。

输入契约声明表、主键、自然键、外键。依赖图按强连通分量（SCC）分组排序；无环部分先父后子。循环 SCC 中每条内部引用边必须显式声明目标能力 `deferrable` 与 `nullable`（布尔值），缺声明或不完整时契约编译以 `reference_cycle: capability_undeclared` 拒绝，非布尔值报 `invalid_capability`。旧 CSV 原始字节、哈希和行号冻结。自然键规范化后派发新键，碰撞与缺父行阻断；引用边保留旧值和新值。目标快照同键同值可复用，异值阻断。计划固定来源、目标与契约摘要，执行前再次核对。

循环组先完成整组 identity_map 与 ReferenceEdge，再按目标能力选择原子应用组：目标边全部 `deferrable` 时整组行一次写入并在提交前验证引用闭合；否则仅当每条环边 `nullable` 时，在单事务内先写暂存实体行（环字段为空）再回填全部环边；既非延期也非 nullable 时整组 `cycle_not_applyable` 阻断。应用在单事务中写入，故障回滚该事务，不留下部分引用；提交前校验失败报 `group_reference_unclosed`。`rollback` 逆序删除本计划插入的目标行。报告含来源、映射、引用边、循环组（成员、策略、回填边）、冲突、步骤。状态：ready→applied/rolled_back，blocked 不能执行。错误码包括 reference_cycle、unknown_reference、natural_collision、orphan_reference、target_conflict、stale_plan、invalid_capability、cycle_not_applyable、group_reference_unclosed。

回滚前重新核对执行步骤、逆操作台账、计划拥有的目标行内容，以及外部目标行是否引用这些行。任一证据已变化时整次回滚拒绝，目标数据保持原样；不会为了恢复旧摘要而删除后来修改的业务数据。相关错误码为 `apply_ledger_changed`、`rollback_ledger_changed`、`rollback_conflict`、`rollback_referenced_by_external_row`。`pytest` 固定从本仓 `src` 导入源码。

来源导入先核对契约声明的主键、自然键和外键列，缺列或空键不会等到计划执行时才失败。已有目标快照同键异值时拒绝导入并保持原行，避免干跑与真实目标之间出现静默覆盖。计划的来源、目标摘要和回滚证据共同决定能否安全落地或撤销。

迁移落地后的目标核对通过 `target-audit` 检查计划拥有的目标行与引用关系，逐项记录缺失、值分歧和断裂引用，形成 complete 或 divergent 结论。计划执行成功仅表示写入事务完成，不代表目标内容后续仍与映射和引用预期相符。

旧键退役通过 `client-contract`、`lookup-observe` 和 `retirement-gate` 决定。关键客户端必须实际观察到映射后的新键及目标载荷哈希，且目标核对仍有效，才能得到 retire_old_keys；否则为 retain_aliases。合同、观测和每次闸门成员证据持久保存。`demo-decisions` 演示完整链路；测试覆盖目标漂移、缺失观测、错误映射与重复序号冲突。
