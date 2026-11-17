# 建仓需求补充 v7：客户贷项、应收抵扣与余额处置

本增量处理“商户因服务问题取得一笔贷项，如何减少未付应收、形成退款义务或转为未来余额”。它使用独立的 `credit_contract`、`credit_note`、`credit_approval`、`credit_decision`、`credit_readjudication`、`credit_application`、`credit_refund`、`credit_revocation` 与 `credit_audit` 账本；只读取已有结算账本对原发票的已付/未付裁定，**从不修改 `settlement_run`、银行回款或原分配边**。它也不读取键迁移、授权、来源归档、分片或预迁移质量表。

## 两项独立业务决定

**贷项处置。** 每客户/币种签定不可变的 `carry` 或 `refund` 合同，并列明出票、审批与退款角色。贷项必须引用同一客户和币种的发票，金额不得超过票面；出票人与审批人不同，且审批须在有效期内。系统从当前结算版本读取原发票未付额，扣除已记的贷项抵扣后，先把新贷项抵扣未付应收；剩余金额按合同形成客户可用余额或待付退款义务。例：I1 票面 100 元、银行已付 80 元、贷项 50 元，得未付抵扣 20 元和余额 30 元。若同一已付清发票取得同额 30 元贷项，`carry` 合同产出余额 30 元，`refund` 合同产出退款义务 30 元；总额和银行身份完全相同，合同取舍改变后续业务动作。

**余额与义务处置。** 客户余额只能在贷项有效期内、对同客户同币种、出票后产生的未来发票应用，且不得超过余额或目标发票当前未付额。退款义务可由授权付款人分笔履行，每笔保留银行证明摘要。未使用余额到期不能再应用；退款义务不因贷项日期到期自动消失。贷项撤销若没有已经发生的抵扣、应用或退款可接受，并允许新贷项带 `supersedes` 纠正；一旦发生经济效果，撤销转 `disputed`，保留旧应用/付款证据，不能静默改写。

## 结算变化合同与追加式重裁

结算版本是追加式的：迟到冲正只会产生新 `settlement_run` 和新的发票裁定，不会改动首次 `credit_decision` 绑定的旧版本。因此合同必须事先声明 `settlement_change_policy`，重裁才不是任意改账：

- `freeze_review`（未声明时的默认值）：原发票的已付/未付在后续版本中改变后，贷项冻结为 `disputed_settlement`。已发生的未来发票应用和已付款退款全部保留，但未用余额和待付退款义务暂停，不能继续应用或支付。若之后的新版本恢复原裁定，可再追加一条 `confirmed` 重裁解除冻结。
- `reallocate_unspent`：只对尚未发生经济效果的金额按当前版本重算。先抵扣当前原发票的银行未付额，受该发票已被其他贷项/应用占用的额度约束；剩余金额继续留在合同原始 disposition 对应的余额或退款义务中。

每次重裁写入独立的 `credit_readjudication`，绑定原 `credit_decision`、前一条重裁（如无则空）、旧/新 `settlement_run`、当前原发票裁定（状态、已付、未付）、合同摘要、处理理由与证据文件 SHA-256。`credit-application`、`credit-refund` 和旧审计都不被改写，旧 `credit_decision` 与旧 `settlement_run` 始终可追溯。重裁在同一事务内核对“原发票抵扣 + 未来贷项应用 + 已付退款 + 当前可用/待付金额 ≤ 贷项面额”；任何证据不符、旧版本/前序记录已变或金额超额都会整体拒绝，两本账不会部分调整。同一 note 的同一 `new_run_id` 只能重裁一次，下一次新版本到来时可追加下一条，形成链式历史。

例：I1 票面 100 元、银行已付 100 元；30 元 carry 贷项首次处置为原发票抵扣 0、客户余额 30 元，其中 20 元已应用到 I3，尚余 10 元。银行迟到冲正 10 元后 I1 当前未付 10 元。`freeze_review` 保留 I3 的 20 元应用、把贷项冻结争议并暂停余下 10 元，I1 仍欠 10 元；`reallocate_unspent` 追加一条重裁，把未用的 10 元抵回 I1，保留 I3 的 20 元，重裁后原发票贷项抵扣 10 元、可用余额 0，I1 的贷项后有效未付为 0。两者都不改写旧 run、首次 `credit_decision` 或 I3 的旧 `credit_application`。

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

`reallocate_unspent` 的正常链可插入迟到冲正后的重裁：

```powershell
python -m keybridge credit-contract tests/fixtures/credit_contract_carry_reallocate.json --db credit.sqlite
python -m keybridge settlement-import tests/fixtures/credit_reversal_i1_10.json --db credit.sqlite
python -m keybridge credit-audit --db credit.sqlite --note CN1 --as-of 2026-11-16T00:00:00Z
python -m keybridge credit-readjudicate readjudication.json --db credit.sqlite
```

重裁证据文件至少包含 `readjudication_id`、`note_id` 与 `reason`，并可附带 `expected_old_run_id`、`expected_contract_sha`、`expected_readjudication_id` 以拒绝陈旧输入。

正常链得到 20 元原发票抵扣、30 元余额；对未来 I3 应用 20 元后剩 10 元，I3 在原结算账本仍显示银行未付 40 元，而贷项账本给出有效未付 20 元。反例覆盖自审、跨客户应用、未来发票未开具时提前应用、余额超用、余额过期、分笔退款超额、已使用贷项撤销争议、迟到银行冲正导致原贷项待复核或待重裁、陈旧/冲突重裁证据、连续结算版本与同一发票多贷项共享未付额度，以及同一事件 ID 异内容重放。所有金额按整数分保存，来源原文和 SHA-256 保留。

签名和银行退款证明在本地模拟器中是输入标签与摘要，未接入外部身份或支付网络。`freeze_review` 冻结后不会自动追回已应用的未来余额或已付退款；`reallocate_unspent` 也只重排尚未发生经济效果的金额，绝不删除或改写旧应用/付款，超出未用额度的部分仍需另行办理补偿。旧结算发票的银行未付值与贷项后的有效未付值应分别报告，不能把客户贷项误称为实际银行回款。
