# 建仓需求补充 v5：来源质量隔离、人工修复与放行

本增量在键迁移、更正、授权、归档和分片恢复之外增加一个**独立的迁移前来源质量闸门**。它处理一批源记录能否作为可导出的数据集，不修改已有迁移计划或目标行。该业务决定与既有“迁移后旧源更正”不同：可疑来源先隔离，经独立复核修复后才可放行给后续导入；未获放行时不会产生导出 CSV。

质量合同冻结目标表、身份字段、必填字段及数值区间。`quality-import` 保存整批输入原始字节、摘要和每行来源 ID，重复来源 ID 当场拒绝。`quality-evaluate` 按合同对缺字段、越界值和批内重复身份逐行裁定，保留可接受与隔离记录；重复身份的两行均隔离，不按到达先后择信。`quality-propose` 对隔离行提交带旧内容摘要的字段修复建议，`quality-review` 只能由不同于提交人的复核者批准或拒绝，批准前再次核对基线摘要。批准后重评整批，因此修复一行也可能让另一行退出重复身份隔离。`quality-release` 只有整批无隔离行时才写 CSV 和 manifest SHA-256；阻断、放行和复核记录均追加保留。

运行样例：

```powershell
python -m keybridge quality-contract tests/fixtures/quality_contract.json --db quality.sqlite
python -m keybridge quality-import tests/fixtures/quality_quarantine.json --contract quality-orders --db quality.sqlite
python -m keybridge quality-evaluate --batch 1 --db quality.sqlite
python -m keybridge quality-release --batch 1 --out released.csv --db quality.sqlite
python -m keybridge quality-report --db quality.sqlite
python -m pytest -q tests/test_quality.py
```

隔离样例有一行金额越界，两行身份冲突；复核者把越界行修为新身份且金额合法后，整批可放行。测试覆盖未经复核不放行、自签拒绝、旧摘要不符、拒绝后维持隔离、重复来源 ID 拒绝与 CSV 实际输出。此本地项目不连接外部权威来源；人工复核身份是输入标签而非密码学认证。导出 CSV 仍须由调用者按具体迁移合同选择目标表，不会自动执行迁移。
