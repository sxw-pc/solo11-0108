# 键桥迁移台

本地多表旧键到新键的确定性迁移与引用重写工具。`python -m keybridge demo` 从合成父子表生成计划、在 SQLite 沙箱应用并输出 JSON/Markdown。`python -m pytest -q` 运行测试。仅需 Python 3.11+ 标准库；不连接真实数据库。

输入契约声明表、主键、自然键、外键。依赖图拓扑排序；旧 CSV 原始字节、哈希和行号冻结。自然键规范化后派发新键，碰撞与缺父行阻断；引用边保留旧值和新值。目标快照同键同值可复用，异值阻断。计划固定来源与目标摘要，执行前再次核对。应用在单事务中写入，故障回滚该事务；`rollback` 逆序删除本计划插入的目标行。报告含来源、映射、引用边、冲突、步骤。状态：ready→applied/rolled_back，blocked 不能执行。错误码包括 reference_cycle、unknown_reference、natural_collision、orphan_reference、target_conflict、stale_plan。

回滚前重新核对执行步骤、逆操作台账、计划拥有的目标行内容，以及外部目标行是否引用这些行。任一证据已变化时整次回滚拒绝，目标数据保持原样；不会为了恢复旧摘要而删除后来修改的业务数据。相关错误码为 `apply_ledger_changed`、`rollback_ledger_changed`、`rollback_conflict`、`rollback_referenced_by_external_row`。`pytest` 固定从本仓 `src` 导入源码。

来源导入先核对契约声明的主键、自然键和外键列，缺列或空键不会等到计划执行时才失败。已有目标快照同键异值时拒绝导入并保持原行，避免干跑与真实目标之间出现静默覆盖。计划的来源、目标摘要和回滚证据共同决定能否安全落地或撤销。
