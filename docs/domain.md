# 领域约定

约束青年田野采集资料的授权、观察和解释分层，保证数字档案入库可追溯。

## 十类资料分层

| 层次 | 聚合对象 | 进入事件 | 说明 |
| --- | --- | --- | --- |
| 培训资格 | `training_qualification` | `COLLECTOR_QUALIFIED` | 采集者先通过伦理与建档培训，才能进入采集计划、上传观察 |
| 采集计划 | `collection_plan` | `COLLECTION_PLAN_APPROVED` | 计划必须挂已登记且有效的地点、已具备资格的采集者 |
| 地点沿革 | `place` | `PLACE_REGISTERED` / `PLACE_ALIAS_RECORDED` / `PLACES_MERGED` | 规范名、异名与合并链；同一地点名称不同先并列登记，核实后再合并 |
| 受访同意 | `consent` | `CONSENT_GRANTED` / `CONSENT_WITHDRAWN` | 同意按版本保存，范围为 `campus_research`（校内研究）与 `public_archive`（公开档案），撤回按版本时刻生效 |
| 原始文件校验 | `source_file` | `RAW_FILE_RECEIVED` | 哈希、字节数、拍摄时间；同哈希只入库一次，观察必须引用已校验文件 |
| 观察记录 | `source_observation` | `OBSERVATION_UPLOADED` | 原始观察一经入库**不可修改、不可覆盖**；纠错另存 `correction` |
| 解释主张 | `archive_claim` | `CLAIM_PROPOSED` | 解释必须引用观察或旧解释；后到的解释可引用旧观察，不能覆盖它 |
| 专业复核 | `review` | `CLAIM_REVIEWED` | 结论 `accepted`/`rejected`；复核人不得复核自己提出的解释或自己采集的材料 |
| 纠错 | `correction` | `CORRECTION_FILED` | 纠错指向观察或解释，原记录保留 |
| 公开范围 | `archive_claim` | `ARCHIVE_RELEASED` 等 | 只有复核通过、且不超出引用链上受访者当前授权范围的解释才能发布 |

另有派生/系统事件：`RESEARCH_USE_RECORDED`（已用于研究）、`ARCHIVE_ANNOTATED`（追加说明）、
`RELEASE_RESTRICTED`（范围限制）、`TRANSCODE_JOB_STARTED`/`TRANSCODE_SHARD_COMPLETED`
（批量转档）、`DISPUTE_OPENED`/`DISPUTE_RESOLVED`（争议队列）。

## 稳定事件键与补传

离线设备生成的 `idempotency_key` 在补传时保持不变。入库关按键处理：

1. 键未见：校验契约与前置条件后入库；
2. 键已见且内容指纹完全一致（`event_id` 信封标识除外）：判定重复，**只入库一次**；
3. 键已见但文件哈希、同意版本等内容不同：**不覆盖**，进入争议队列
   （`same_key_file_differs` / `same_key_consent_version_differs`），等待人工结案。

## 关键业务规则

- **观察不可变**：解释、纠错、撤回都不会改写 `OBSERVATION_UPLOADED` 的原始内容；
  解释沿 `source_refs` 引用观察与旧解释（引用闭包 `source_closure`）。
- **地点合并只迁移仍有效的关系**：`PLACES_MERGED` 时，被合并地点上的观察，
  仅当对应受访者在合并时刻仍有有效同意范围才迁移到保留地点；其余记入
  `rejected_relations`，不迁移。被合并地点失活，新观察必须挂保留地点。
- **同意撤回沿引用链级联**：受访者撤回某范围后，引用链触及该受访者观察的全部解释：
  - 尚未发布：登记 `RELEASE_RESTRICTED`，此后不得发布到被撤回范围；
  - 已发布：保留发布事实，登记范围限制并 `ARCHIVE_ANNOTATED` 加注；
  - 已用于研究（`RESEARCH_USE_RECORDED`）：原始记录保留，追加“已用于研究”的说明。
- **回避**：`CLAIM_REVIEWED` 校验复核人既非解释提出者，也不是引用链上任一观察的采集者。
- **发布门禁**：`ARCHIVE_RELEASED` 要求解释复核通过、范围在全部相关受访者当前授权之内、
  且未被撤回限制；同一范围重复发布被拒绝（补传应复用稳定事件键）。
- **断点续转**：转码批次记录分片总数与已完成分片；崩溃后用 `resume` 得到
  第一个未完成分片并继续。同一分片相同输出哈希重放安全，不同哈希被拒绝。

## 审计

`audit <ledger> <claim_id>` 从一条公开叙述回溯：引用链上的原始观察与文件校验、
受访者授权授予/撤回与当前有效范围、地点沿革（含合并迁移与拒绝迁移）、专业复核、
纠错、发布/研究使用/加注/限制，以及**针对同一批原始材料被否决的全部解释**和相关争议。

## 交换层边界

事件信封字段（`event_id`、枚举、带时区时间、从 1 递增的版本、各事件必需载荷、
`idempotency_key`）由契约校验负责；幂等、争议、引用链、合并、级联等属于业务层
（`gate.py`），校验层不替调用方改写输入。所有时间必须携带时区。
