# 青年田野资料入库关

约束青年田野采集资料的授权、观察和解释分层，保证数字档案入库可追溯。

## 背景

高校学生返乡采集古城建筑、口述史和巡检照片后，材料常出现：同一地点名称不同、
受访者只同意校内研究、影像时间晚于建筑修缮等情况。入库关把**原始观察**与
**解释主张**分层保存，避免推测混入原始档案，并对离线补传、地点合并、授权
撤回、专业复核、批量转档与审计追溯给出可重放的规则。

## 目录

- `contracts/domain.schema.json`：事件信封、聚合类型、事件类型与载荷约定。
- `data/sample.json`：单事件校验样例。
- `data/ledger.json`：可直接审计的完整中文场景账本（鼓楼：异名、校内同意、照片晚于修缮、撤回级联、断点续转）。
- `data/offline_batch.json`：离线设备补传批次（一条完全重复、一条同键异文）。
- `scripts/build_sample.py`：样例生成脚本（以门禁实际结果为准）。
- `src/heritage_field_intake/`：契约校验、入库关门禁（`gate.py`）、审计回溯（`audit.py`）与命令行入口。
- `tests/`：契约边界与全部业务规则测试（33 项）。
- `docs/domain.md`：分层、事件语义与业务规则。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests scripts
```

## 命令行

```bash
# 单事件契约校验
PYTHONPATH=src python3 -m heritage_field_intake.cli validate contracts/domain.schema.json data/sample.json
# 旧用法（两参数）仍兼容
PYTHONPATH=src python3 -m heritage_field_intake.cli contracts/domain.schema.json data/sample.json

# 离线补传：完全相同只入库一次；同键异文进争议队列；结果回写账本
PYTHONPATH=src python3 -m heritage_field_intake.cli ingest /tmp/ledger.json data/offline_batch.json

# 未决争议
PYTHONPATH=src python3 -m heritage_field_intake.cli disputes /tmp/ledger.json

# 批量转档崩溃后，从第一个未完成分片继续
PYTHONPATH=src python3 -m heritage_field_intake.cli resume data/ledger.json job-t1
# → {"job_id": "job-t1", "next_shard": 2, "pending": [2, 3]}

# 从一条公开叙述返回原始采集、授权、复核、所有被否决解释与限制说明
PYTHONPATH=src python3 -m heritage_field_intake.cli audit data/ledger.json claim-D
```

`ingest` 逐事件输出 `accepted` / `duplicate` / `disputed` / `rejected`；存在
`rejected` 时以非零状态结束。账本以事件数组持久化，重启后从事件流重放投影。

## 关键规则速查

- 十类资料分层：培训资格、采集计划、地点沿革、受访同意版本、原始文件校验、
  观察记录、解释主张、专业复核、纠错、公开范围。
- 稳定事件键：同键同文只入库一次；同键异文（文件/同意范围不同）进争议队列，不覆盖。
- 观察一经入库不可修改；后来的解释只能引用，不能覆盖；纠错另存。
- 地点合并只迁移合并时仍有有效同意的观察关系，其余记 `rejected_relations`。
- 受访者撤回公开许可：未发布的沿引用链限制发布；已发布/已用于研究的保留记录并追加说明。
- 复核人不得复核自己提交或采集的材料；批量转档从最小未完成分片续跑。
- 审计输出覆盖原始采集、授权链、复核、纠错、公开范围变迁与全部被否决解释。
