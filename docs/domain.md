# 领域约定

约束青年田野采集资料的授权、观察和解释分层，保证数字档案入库可追溯。

聚合对象包括`field_campaign`、`place_identity`、`source_observation`、`archive_claim`。事件类型包括`COLLECTOR_QUALIFIED`、`OBSERVATION_UPLOADED`、`CLAIM_REVIEWED`、`CONSENT_WITHDRAWN`、`ARCHIVE_RELEASED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `OBSERVATION_UPLOADED`：还需包含 `file_hash`, `consent_version`。
- `CLAIM_REVIEWED`：还需包含 `reviewer_id`, `source_refs`。
- `CONSENT_WITHDRAWN`：还需包含 `effective_at`, `affected_scope`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
