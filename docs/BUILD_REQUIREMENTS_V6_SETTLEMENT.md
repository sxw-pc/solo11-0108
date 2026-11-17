# 建仓需求补充 v6：多源回款对账与结算账本

## 独立业务目标

账单来源、银行回款、人工分配凭证和迟到冲正分别作为不可变事件入库。结算账本回答每笔应收是否结清、每笔回款实际分配到哪里，以及当前结论为何改变。它不读取迁移计划、键映射、来源质量放行或归档副本，也不改变这些流程的状态。

金额输入按十进制转换为整数分；半分、负数和零被拒绝。导入保留原始文件字节与 SHA-256、单事件原文与摘要；同一 `event_id` 只有类别和正文均一致才能幂等重放。每次事件集变化产生新 `settlement_run` 和独立的边、账单裁定、回款裁定及问题记录，旧裁定保留。

## 四类证据与裁定

- `invoices` 是应收，`payments` 是银行实收。双方的 `party`、`reference` 和 `currency` 必须一致。
- `allocations` 是人工分配凭证，允许一笔回款拆给多笔应收，也允许一笔应收由多笔回款覆盖。分配额超过回款净额、跨主体/引用/币种，或多笔边合计超过应收，进入争议而不静默截断。
- `reversals` 是迟到冲正，可整笔冲正，也可指明 `invoice_id` 只冲某一分配边。已拆分回款的部分冲正若没有分配边归属，系统拒绝推断并记录 `ambiguous_reversal`。冲正后重新计算净额和未结金额，旧结算版本不改写。
- 没有人工分配凭证时，仅对唯一可判定的同身份剩余应收作自动分配。单个候选可部分分配；多个候选只有唯一精确子集才可分配。存在多种同额组合时留下 `ambiguous_match`，不得按输入顺序选一个。候选多于 16 笔时也保持待裁定，避免指数枚举误当确定结果。

`settlement_run.status` 为 `settled`、`open` 或 `disputed`。全体应收结清仍有未匹配或未分配净回款时，不能报 `settled`。报告包含源文件摘要、历次结算版本、分配边、逐账单/回款状态和争议原因。

## 可运行样例与反例

```powershell
python -m pip install -e .
python -m keybridge demo-settlement --workdir .benchmarks/settlement-demo
python -m keybridge settlement-import tests/fixtures/settlement_ambiguous.json --db .benchmarks/ambiguous.db
python -m keybridge settlement-report --db .benchmarks/ambiguous.db
python -m pytest -q
```

正常样例 `settlement_ok.json`：P1 的 120 元拆给 I1 80 元和 I2 40 元；P2 的 30 元拆给 I1 20 元和 I2 10 元。两笔应收分别由两笔回款覆盖，四条边同时成立，不能用单键直连表达。`settlement_reversal.json` 在结清后冲正 P2，I1/I2 分别重新欠 20/10 元，旧 `settled` 版本仍可查询。`settlement_attributed_reversal.json` 只冲正 P1→I1 的 20 元，I2 仍结清。

反例 `settlement_ambiguous.json` 的应收总额和回款总额均为 300 元，但同一 150 元回款可对应 100+50 或 90+60，不能用总额相等或输入顺序判为结清。`settlement_wrong_party.json` 也有总额相等的 150 元，应收方却不同，跨主体的 50 元保持未分配。测试还覆盖拆分回款无归属的部分冲正、已全额结清时额外未匹配回款、事件类别复用、金额变更重放和非整分拒绝。

## 边界

此账本是可复现的本地 SQLite 业务模拟器。它不对接真实银行；自动匹配刻意保守，不能把缺失的分配凭证补写成事实。输入文件内的分配凭证须引用已存在或同文件先导入的账单和回款。
