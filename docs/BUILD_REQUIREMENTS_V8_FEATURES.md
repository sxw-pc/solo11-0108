# 建仓需求补充 v8：训练特征与在线证据一致性

本流程给离线风控建模团队维护一套独立的特征训练发布账本。它不读取或修改键迁移、归档分片、来源质量、回款或客户贷项表。业务决定是：某个预测时点哪些特征当时**已经可用**，对应标签是否具备训练资格，离线训练行与实际在线捕获是否一致，最终训练版本能否发布。

`feature-contract` 固定至少两个特征的 TTL、必需性与最小合格样本数。`feature-import` 记录观测时间、可用时间、修订、撤回及原始文件字节与摘要；同一观测同版异内容拒绝，递增版次须晚于前版可用时间。`feature-labels` 固定预测时点、结果时点和结果被获知时点。`feature-train` 对每个标签按预测时点选择当时已可用且未过期的特征，结果若在构建冻结时刻尚未知也不可训练；每行保存值、观测 ID、修订及排除原因。迟到修订不可倒灌历史训练行。

`feature-capture` 接收独立的在线服务捕获，记录同一预测时点的实体、特征值和观测版次。`feature-parity` 比较训练行与在线捕获的值及来源版次，缺失或不同为 mismatch。`feature-release` 只接受合格训练版本与 matched 核对，冻结发布清单摘要。旧版本、失败核对与历史证据保留，不能以新的捕获覆盖。`feature-report` 输出上述账本。

可复现反例：A 的余额观测 08:00 产生、09:00 可用，10:00 预测时可引用；B 的同类观测 08:00 产生、12:00 才可用，即使后续构建在 11 月 3 日也必须排除 B，不能把未来才可用的 200 回填进训练。在线捕获若把 A 的 orders=4 错记为 5，发布闸口拒绝；正确值与观测版次一致才可发布。11 月 4 日到达的 A 余额修订不能改变 11 月 1 日 10:00 的已冻结训练解释。

运行：

```powershell
python -m keybridge feature-contract tests/fixtures/feature_contract.json --db feature.sqlite
python -m keybridge feature-import tests/fixtures/feature_observations.json --db feature.sqlite
python -m keybridge feature-labels tests/fixtures/feature_labels.json --db feature.sqlite
python -m keybridge feature-train --db feature.sqlite --contract risk-v1 --freeze-at 2026-11-03T00:00:00Z
python -m keybridge feature-capture tests/fixtures/feature_capture_ok.json --db feature.sqlite
python -m keybridge feature-parity --db feature.sqlite --build 1 --capture online-1
python -m keybridge feature-release --db feature.sqlite --build 1 --parity 1
python -m keybridge feature-report --db feature.sqlite --build 1
python -m pytest -q tests/test_feature_pipeline.py
```

外部身份签名、实时在线服务与模型训练器未接入；在线数据由离线捕获夹具模拟，发布只是本地训练清单许可，不声称生产模型已上线。
