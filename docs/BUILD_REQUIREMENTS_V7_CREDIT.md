# 建仓需求补充 v7：客户贷项、应收抵扣与余额处置

本增量处理“商户因服务问题取得一笔贷项，如何减少未付应收、形成退款义务或转为未来余额”。它使用独立的 `credit_contract`、`credit_note`、`credit_approval`、`credit_decision`、`credit_application`、`credit_refund`、`credit_revocation` 与 `credit_audit` 账本；只读取已有结算账本对原发票的已付/未付裁定，**从不修改 `settlement_run`、银行回款或原分配边**。它也不读取键迁移、授权、来源归档、分片或预迁移质量表。

## 两项独立业务决定

**贷项处置。** 每客户/币种签定不可变的 `carry` 或 `refund` 合同，并列明出票、审批与退款角色。贷项必须引用同一客户和币种的发票，金额不得超过票面；出票人与审批人不同，且审批须在有效期内。系统从当前结算版本读取原发票未付额，扣除已记的贷项抵扣后，先把新贷项抵扣未付应收；剩余金额按合同形成客户可用余额或待付退款义务。例：I1 票面 100 元、银行已付 80 元、贷项 50 元，得未付抵扣 20 元和余额 30 元。若同一已付清发票取得同额 30 元贷项，`carry` 合同产出余额 30 元，`refund` 合同产出退款义务 30 元；总额和银行身份完全相同，合同取舍改变后续业务动作。

**余额与义务处置。** 客户余额只能在贷项有效期内、对同客户同币种、出票后产生的未来发票应用，且不得超过余额或目标发票当前未付额。退款义务可由授权付款人分笔履行，每笔保留银行证明摘要。未使用余额到期不能再应用；退款义务不因贷项日期到期自动消失。贷项撤销若没有已经发生的抵扣、应用或退款可接受，并允许新贷项带 `supersedes` 纠正；一旦发生经济效果，撤销转 `disputed`，保留旧应用/付款证据，不能静默改写。原发票在贷项决定后发生迟到银行冲正时，余额进入 `disputed_settlement` 并暂停继续使用，旧贷项决定和旧结算版本均保留。

## 离线运行与反例

```powershell
python -m keybridge settlement-import tests/fixtures/credit_partial_settlement.json --db credit.sqlite
python -m keybridge credit-contract tests/fixtures/credit_contract_carry.json --db credit.sqlite
python -m keybridge credit-note tests/fixtures/credit_note_carry.json --db credit.sqlite
python -m keybridge credit-review tests/fixtures/credit_review_carry.json --db credit.sqlite
python -m keybridge credit-decide --db credit.sqlite --note CN1 --as-of 2026-11-07T00:00:00Z
python -m keybridge settlement-import tests/fixtures/credit_future_invoice.json --db credit.sqlite
python -m keybridge credit-apply tests/fixtures/credit_application_20.json --db credit.sqlite
python -m keybridge credit-audit --db credit.sqlite --note CN1 --as-of 2026-11-13T00:00:00Z
python -m keybridge credit-report --db credit.sqlite --note CN1
python -m pytest -q tests/test_credit.py
```

正常链得到 20 元原发票抵扣、30 元余额；对未来 I3 应用 20 元后剩 10 元，I3 在原结算账本仍显示银行未付 40 元，而贷项账本给出有效未付 20 元。反例覆盖自审、跨客户应用、未来发票未开具时提前应用、余额超用、余额过期、分笔退款超额、已使用贷项撤销争议、迟到银行冲正导致原贷项待复核，以及同一事件 ID 异内容重放。所有金额按整数分保存，来源原文和 SHA-256 保留。

签名和银行退款证明在本地模拟器中是输入标签与摘要，未接入外部身份或支付网络。当前迟到银行冲正后的贷项只标争议并阻止后续使用，不自动追回已应用的未来余额；已发生经济效果需要另行办理补偿，不会被静默擦除。旧结算发票的银行未付值与贷项后的有效未付值应分别报告，不能把客户贷项误称为实际银行回款。
