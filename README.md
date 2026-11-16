# 键桥迁移台

本地多表旧键到新键的确定性迁移与引用重写工具。`python -m keybridge demo` 从合成父子表生成计划、在 SQLite 沙箱应用并输出 JSON/Markdown。`python -m pytest -q` 运行测试。仅需 Python 3.11+ 标准库；不连接真实数据库。

输入契约声明表、主键、自然键、外键。依赖图拓扑排序；旧 CSV 原始字节、哈希和行号冻结。自然键规范化后派发新键，碰撞与缺父行阻断；引用边保留旧值和新值。目标快照同键同值可复用，异值阻断。计划固定来源与目标摘要，执行前再次核对。应用在单事务中写入，故障回滚该事务；`rollback` 逆序删除本计划插入的目标行。报告含来源、映射、引用边、冲突、步骤。状态：ready→applied/rolled_back，blocked 不能执行。错误码包括 reference_cycle、unknown_reference、natural_collision、orphan_reference、target_conflict、stale_plan。

回滚前重新核对执行步骤、逆操作台账、计划拥有的目标行内容，以及外部目标行是否引用这些行。任一证据已变化时整次回滚拒绝，目标数据保持原样；不会为了恢复旧摘要而删除后来修改的业务数据。相关错误码为 `apply_ledger_changed`、`rollback_ledger_changed`、`rollback_conflict`、`rollback_referenced_by_external_row`。`pytest` 固定从本仓 `src` 导入源码。

执行合同分 atomic 与 checkpointed 两种。atomic 是默认：全部动作在单个 SQLite 事务中写入，中途失败整体回滚，目标摘要完全复原。checkpointed 需显式选择（`apply --mode checkpointed`）：按拓扑安全的表边界分批，每批把目标写入、逆操作账本、apply_step、游标（plan_cursor）和动作摘要（apply_batch）放在同一事务提交。父表批已提交、子表批未执行时进程中断，计划状态为 applying，报告列明可用表与待执行表，业务方可先使用父表数据，但不会把部分迁移标记为完成。`resume` 对同一 plan_id 续跑：按批序号与动作摘要幂等跳过已提交批，并核对这些目标行、apply_step 与 rollback_entry 仍吻合；来源、契约、未执行范围或已完成行被外部修改时分别以 `stale_plan`、`contract_changed`、`resume_scope_changed`、`resume_completed_changed` 拒绝续跑，且不覆盖外部变化。对 applying 计划执行 `rollback` 会按逆依赖顺序回滚已提交前缀，验证计划归属与外部引用保护后状态回到 ready，可重新执行。

来源导入先核对契约声明的主键、自然键和外键列，缺列或空键不会等到计划执行时才失败。已有目标快照同键异值时拒绝导入并保持原行，避免干跑与真实目标之间出现静默覆盖。计划的来源、目标摘要和回滚证据共同决定能否安全落地或撤销。

迁移落地后的目标核对通过 `target-audit` 检查计划拥有的目标行与引用关系，逐项记录缺失、值分歧和断裂引用，形成 complete 或 divergent 结论。计划执行成功仅表示写入事务完成，不代表目标内容后续仍与映射和引用预期相符。

旧键退役通过 `client-contract`、`lookup-observe` 和 `retirement-gate` 决定。关键客户端必须实际观察到映射后的新键及目标载荷哈希，且目标核对仍有效，才能得到 retire_old_keys；否则为 retain_aliases。合同、观测和每次闸门成员证据持久保存。`demo-decisions` 演示完整链路；测试覆盖目标漂移、缺失观测、错误映射与重复序号冲突。
