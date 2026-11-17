# 建仓需求补充 v4：离线分片与实际恢复

完整副本回执只能记录保管方的摘要声明，不能证明手头的字节能拼回原对象。本增量在原有保留计划和完整副本核验之外，实现一个独立的 **2-of-3 XOR 分片**离线恢复路径。它针对单一已保留候选对象，把原始字节分成两片等长数据片和一片异或校验片；三个索引中任意两个不同索引、来自两个不同故障域的正确片，可重建原对象。它不声称支持通用 k-of-n 编码。

`shard-manifest` 读取真实来源文件，先核对整对象 SHA-256 与已保留候选摘要，再生成三份分片及其哈希，并把对象大小、摘要、分片摘要、候选、归档计划及 manifest 版次固化。不能拿计划外批次或伪造对象创建 manifest。`shard-ingest` 录入故障域、索引与真实分片字节；重复 proof ID 仅允许同一证据重放。`shard-restore` 可模拟一个或多个故障域丢失，排除这些域后挑选独立域的不同分片索引，实际拼回字节并核对整对象 SHA-256。结果与丢失域、所用索引、重建摘要、原因及输出文件路径写入审计账。幸存域里出现摘要冲突时结论为 `disputed`；相同索引或相同域的两张凭据不能冒充可解码组合，结论为 `insufficient`。重建字节或整对象摘要不符为 `corrupt`。

manifest 新版出现后旧版恢复结论为 `stale`，候选集合修订导致原计划过期也为 `stale`。旧审计保留原状，新运行追加审计。完整副本流程仍可独立使用；分片恢复不会把完整副本回执自动视为真实字节证明。

离线样例可运行 `python -m pytest -q tests/test_shards.py`；测试从真实字节和 SHA 创建保留候选，分别丢失三个故障域之一后恢复相同原文，并覆盖同索引、同域、冲突字节、manifest 版次及候选版本失效。CLI 命令为：

```text
python -m keybridge shard-manifest --db archive.sqlite --plan 1 --candidate source-1 --revision 1 --input source.csv --out parts
python -m keybridge shard-ingest --db archive.sqlite --manifest 1 --proof p0 --index 0 --domain site-a parts/source-1.v1.p0.bin
python -m keybridge shard-ingest --db archive.sqlite --manifest 1 --proof p1 --index 1 --domain site-b parts/source-1.v1.p1.bin
python -m keybridge shard-restore --db archive.sqlite --manifest 1 --lost site-c --out recovered.csv
python -m keybridge shard-report --db archive.sqlite
```

这里的故障域是离线输入标签，尚无外部域身份认证；恢复判断依据入库字节与哈希，不代表远程存储实际可访问性。
