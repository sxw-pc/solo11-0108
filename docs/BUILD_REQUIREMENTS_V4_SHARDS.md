# 建仓需求补充 v4：离线分片与实际恢复

完整副本回执只能记录保管方的摘要声明，不能证明手头的字节能拼回原对象。本增量在原有保留计划和完整副本核验之外，实现一个独立的离线分片恢复路径。除原有的 **2-of-3 XOR** 编码外，新增系统化 **MDS k-of-n** 编码合同（`1 ≤ k < n ≤ 32`）：原始字节按 k 份数据片切分并以零补齐，生成 n 份系统性分片（前 k 份即数据片），任意 k 个不同索引且来自不同故障域的满秩分片可重建原对象。编码参数、生成矩阵和全部分片摘要随 manifest 一并冻结。

`shard-manifest` 读取真实来源文件，先核对整对象 SHA-256 与已保留候选摘要，再生成分片及其哈希，并把对象大小、摘要、编码器 id、k、n、GF(256) 生成矩阵、n 份分片摘要、候选、归档计划及 manifest 版次固化。默认仍为 `xor-2of3`；显式给出 `--k/--n`（或 `--codec mds-vandermonde-gf256`）则使用系统化 MDS。不能拿计划外批次或伪造对象创建 manifest；同一 plan/candidate 的同一 revision 只能重放出完全相同的对象与编码参数。

`shard-ingest` 录入故障域、索引与真实分片字节；新 manifest 的分片索引必须落在 `0..n-1`，proof 额外绑定 manifest revision，重复 proof ID 仅允许同一证据重放。`shard-restore` 可模拟一个或多个故障域丢失，排除这些域后按索引和独立故障域筛选幸存 proof，再用对应生成矩阵行求逆恢复数据片、裁掉 padding，并同时核对 manifest 的 `object_sha` 与 archive_candidate 摘要。不能只按“剩下至少 k 个不同索引”判定可恢复：所选 k 行必须满秩；若所有 k 组合都奇异则为 `insufficient`。结果与丢失域、所用索引、编码参数、重建摘要、原因及输出文件路径写入审计账。

幸存域里出现与 manifest 冻结摘要不符的分片时为 `disputed`；相同索引或相同故障域的多张凭据不能互相补充为独立分片，凑不齐满秩组合时为 `insufficient`；重建字节或整对象摘要不符为 `corrupt`。不可逆组合（人为篡改矩阵）同样归为 `insufficient`，不会用奇异行解码出错误字节。

旧的三列 `part0_sha/part1_sha/parity_sha` manifest（`codec_id` 为空）继续按 2-of-3 XOR 原算法读取，与新版共存；`shard-report` 对两类 manifest 都列出 codec/k/n、生成矩阵（旧版为 XOR 矩阵）、分片摘要，并在每次恢复审计上带出编码参数、所用片索引与恢复摘要。

manifest 新版出现后旧版恢复结论为 `stale`，候选集合修订导致原计划过期也为 `stale`。旧审计保留原状，新运行追加审计。完整副本流程仍可独立使用；分片恢复不会把完整副本回执自动视为真实字节证明。

离线样例可运行 `python -m pytest -q tests/test_shards.py tests/test_shards_kofn.py`；测试从真实字节和 SHA 创建保留候选，覆盖 2-of-3 与 3-of-6 的对象往返、丢域后所有 k 索引组合、奇偶长度补齐、重复索引/故障域、manifest 版次与候选计划失效、坏分片与摘要冲突，以及旧三列 manifest 兼容。CLI 命令为：

```text
python -m keybridge shard-manifest --db archive.sqlite --plan 1 --candidate source-1 --revision 1 --input source.csv --out parts
python -m keybridge shard-manifest --db archive.sqlite --plan 1 --candidate source-1 --revision 1 --input source.csv --out parts --codec mds-vandermonde-gf256 --k 3 --n 6
python -m keybridge shard-ingest --db archive.sqlite --manifest 1 --proof p0 --index 0 --domain site-a parts/source-1.v1.p0.bin
python -m keybridge shard-ingest --db archive.sqlite --manifest 1 --proof p1 --index 1 --domain site-b parts/source-1.v1.p1.bin
python -m keybridge shard-restore --db archive.sqlite --manifest 1 --lost site-c --out recovered.csv
python -m keybridge shard-report --db archive.sqlite
```

这里的故障域是离线输入标签，尚无外部域身份认证；恢复判断依据入库字节与哈希，不代表远程存储实际可访问性。存储预算不同：`xor-2of3` 约占对象 1.5 倍、可容忍任一故障域丢失；`mds-3of6` 约占 2 倍、可容忍任意三个故障域丢失，由业务方按预算和可承受故障数选择。

