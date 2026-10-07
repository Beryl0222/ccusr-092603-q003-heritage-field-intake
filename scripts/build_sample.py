"""生成中文联调样例：data/ledger.json 与 data/offline_batch.json。

场景：古城鼓楼田野资料
- 同一地点“鼓楼/谯楼/古楼”异名，后两者并入规范地点；
- 受访者王老仅同意校内研究，陈师傅曾同意公开后撤回；
- 巡检照片拍摄晚于修缮完工，据此主张“修缮前原状”的解释被复核否决；
- 离线设备补传：完全相同只入库一次，同键不同同意版本进入争议队列；
- 批量转档在第 2 片崩溃，等待 resume 从未完成分片继续。

运行：PYTHONPATH=src python3 scripts/build_sample.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_field_intake.gate import Gate  # noqa: E402

schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text(encoding="utf-8"))
gate = Gate(schema)

_seq = 0


def ev(etype, aggregate_type, aggregate_id, payload, occurred_at, key=None, version=1):
    global _seq
    _seq += 1
    item = {
        "event_id": f"evt-{_seq:04d}",
        "event_type": etype,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": payload,
    }
    if key:
        item["idempotency_key"] = key
    result = gate.ingest(item)
    assert result.ok, (etype, result.status, result.issues)
    return item


T = "2026-{t}"

# 1. 培训资格
ev("COLLECTOR_QUALIFIED", "training_qualification", "qual-s001",
   {"collector_id": "s001", "qualified_at": T.format(t="03-01T09:00:00+08:00"),
    "training_session": "2026春田野伦理与建档培训"},
   T.format(t="03-01T09:00:00+08:00"), key="qual:s001")
ev("COLLECTOR_QUALIFIED", "training_qualification", "qual-s002",
   {"collector_id": "s002", "qualified_at": T.format(t="03-01T09:00:00+08:00")},
   T.format(t="03-01T09:00:00+08:00"), key="qual:s002")
ev("COLLECTOR_QUALIFIED", "training_qualification", "qual-r001",
   {"collector_id": "r001", "qualified_at": T.format(t="03-01T09:00:00+08:00"),
    "role": "专业复核"},
   T.format(t="03-01T09:00:00+08:00"), key="qual:r001")

# 2. 地点登记与异名
ev("PLACE_REGISTERED", "place", "place-gulou",
   {"canonical_name": "鼓楼"}, T.format(t="03-05T10:00:00+08:00"), key="place:gulou")
ev("PLACE_ALIAS_RECORDED", "place", "place-gulou",
   {"alias_name": "谯楼", "source": "清代县志"}, T.format(t="03-05T10:05:00+08:00"))
ev("PLACE_REGISTERED", "place", "place-gulou-old",
   {"canonical_name": "古楼", "note": "本地口读音近，暂按新地点登记待核"},
   T.format(t="03-05T10:10:00+08:00"), key="place:gulou-old")

# 3. 受访同意版本
ev("CONSENT_GRANTED", "consent", "consent-p1",
   {"participant_id": "p1-wang", "consent_version": "consent-v1-2026",
    "scope": ["campus_research"],
    "granted_at": T.format(t="03-06T14:00:00+08:00"),
    "note": "王老：仅同意校内研究，不同意公开展示影像"},
   T.format(t="03-06T14:00:00+08:00"), key="consent:p1:v1")
ev("CONSENT_GRANTED", "consent", "consent-p2",
   {"participant_id": "p2-chen", "consent_version": "consent-v1-2026",
    "scope": ["campus_research", "public_archive"],
    "granted_at": T.format(t="03-06T15:00:00+08:00")},
   T.format(t="03-06T15:00:00+08:00"), key="consent:p2:v1")

# 4. 采集计划
ev("COLLECTION_PLAN_APPROVED", "collection_plan", "plan-2026-gulou",
   {"plan_id": "plan-2026-gulou", "place_ids": ["place-gulou", "place-gulou-old"],
    "collector_ids": ["s001", "s002"],
    "approved_at": T.format(t="03-07T09:30:00+08:00")},
   T.format(t="03-07T09:30:00+08:00"), key="plan:2026-gulou")

# 5. 原始文件校验（设备离线采集后统一入库）
ev("RAW_FILE_RECEIVED", "source_file", "file-oral-01",
   {"file_hash": "sha256:1111111111111111", "file_size": 48_213_402,
    "captured_at": T.format(t="08-03T10:20:00+08:00"), "media_type": "audio/m4a"},
   T.format(t="08-05T08:00:00+08:00"), key="dev-s001:file-oral-01")
ev("RAW_FILE_RECEIVED", "source_file", "file-photo-01",
   {"file_hash": "sha256:2222222222222222", "file_size": 7_340_021,
    "captured_at": T.format(t="08-01T16:40:00+08:00"), "media_type": "image/jpeg",
    "note": "鼓楼北面外观；建筑修缮完工于 2026-06-30，影像晚于修缮"},
   T.format(t="08-05T08:05:00+08:00"), key="dev-s002:file-photo-01")
ev("RAW_FILE_RECEIVED", "source_file", "file-inspect-01",
   {"file_hash": "sha256:3333333333333333", "file_size": 9_812_554,
    "captured_at": T.format(t="08-04T09:15:00+08:00"), "media_type": "image/jpeg"},
   T.format(t="08-05T08:10:00+08:00"), key="dev-s002:file-inspect-01")

# 6. 观察记录（原始观察，此后不可修改）
ev("OBSERVATION_UPLOADED", "source_observation", "obs-oral-01",
   {"collector_id": "s001", "plan_id": "plan-2026-gulou", "place_id": "place-gulou",
    "participant_id": "p1-wang", "file_hash": "sha256:1111111111111111",
    "consent_version": "consent-v1-2026",
    "captured_at": T.format(t="08-03T10:20:00+08:00"),
    "transcript_excerpt": "鼓楼清代叫谯楼，木构听老辈说是1948年重修过。"},
   T.format(t="08-05T09:00:00+08:00"), key="dev-s001:obs-oral-01")
ev("OBSERVATION_UPLOADED", "source_observation", "obs-photo-01",
   {"collector_id": "s002", "plan_id": "plan-2026-gulou", "place_id": "place-gulou-old",
    "participant_id": "p2-chen", "file_hash": "sha256:2222222222222222",
    "consent_version": "consent-v1-2026",
    "captured_at": T.format(t="08-01T16:40:00+08:00")},
   T.format(t="08-05T09:05:00+08:00"), key="dev-s002:obs-photo-01")
ev("OBSERVATION_UPLOADED", "source_observation", "obs-inspect-01",
   {"collector_id": "s002", "plan_id": "plan-2026-gulou", "place_id": "place-gulou-old",
    "participant_id": "p2-chen", "file_hash": "sha256:3333333333333333",
    "consent_version": "consent-v1-2026",
    "captured_at": T.format(t="08-04T09:15:00+08:00")},
   T.format(t="08-05T09:10:00+08:00"), key="dev-s002:obs-inspect-01")

# 7. 地点合并：仅迁移仍有效关系（此时两受访者授权均有效）
ev("PLACES_MERGED", "place", "place-gulou",
   {"surviving_place_id": "place-gulou", "absorbed_place_ids": ["place-gulou-old"],
    "reason": "经走访核实“古楼”为“鼓楼”口传异写"},
   T.format(t="08-10T11:00:00+08:00"), key="merge:gulou:2026")

# 8. 解释主张与专业复核
# 8a. 时间矛盾的解释被否决
ev("CLAIM_PROPOSED", "archive_claim", "claim-A",
   {"proposer_id": "s001", "source_refs": ["obs-photo-01"],
    "statement": "这张照片记录了鼓楼修缮前的北侧原状。",
    "proposed_at": T.format(t="08-12T10:00:00+08:00")},
   T.format(t="08-12T10:00:00+08:00"), key="claim:A")
ev("CLAIM_REVIEWED", "review", "review-A",
   {"claim_id": "claim-A", "reviewer_id": "r001", "decision": "rejected",
    "source_refs": ["obs-photo-01"],
    "rationale": "照片拍摄于 2026-08-01，而修缮 2026-06-30 已完工，不可能记录修缮前原状。",
    "reviewed_at": T.format(t="08-15T14:00:00+08:00")},
   T.format(t="08-15T14:00:00+08:00"), key="review:A")

# 8b. 口述解释通过（仅校内范围，因受访者只同意校内研究）
ev("CLAIM_PROPOSED", "archive_claim", "claim-B",
   {"proposer_id": "s002", "source_refs": ["obs-oral-01"],
    "statement": "鼓楼清代称谯楼；口述线索指向 1948 年曾重修木构。",
    "proposed_at": T.format(t="08-16T10:00:00+08:00")},
   T.format(t="08-16T10:00:00+08:00"), key="claim:B")
ev("CLAIM_REVIEWED", "review", "review-B",
   {"claim_id": "claim-B", "reviewer_id": "r001", "decision": "accepted",
    "source_refs": ["obs-oral-01"],
    "rationale": "与县志别名记载互证；1948 年重修表述为口述线索，发布时保留措辞。",
    "reviewed_at": T.format(t="08-20T14:00:00+08:00")},
   T.format(t="08-20T14:00:00+08:00"), key="review:B")

# 8c. 后来的解释引用旧观察与旧解释，不覆盖
ev("CLAIM_PROPOSED", "archive_claim", "claim-C",
   {"proposer_id": "s001",
    "source_refs": ["obs-oral-01", "obs-inspect-01", "claim-B"],
    "statement": "综合口述与巡检照片，1948 年重修范围可能包含北面墙体。",
    "proposed_at": T.format(t="08-26T10:00:00+08:00")},
   T.format(t="08-26T10:00:00+08:00"), key="claim:C")
ev("CLAIM_REVIEWED", "review", "review-C",
   {"claim_id": "claim-C", "reviewer_id": "r001", "decision": "accepted",
    "source_refs": ["obs-oral-01", "obs-inspect-01"],
    "rationale": "巡检照片北墙构件与口述年代线索相容，措辞保持“可能”。",
    "reviewed_at": T.format(t="08-28T14:00:00+08:00")},
   T.format(t="08-28T14:00:00+08:00"), key="review:C")

# 8d. 仅基于陈师傅材料的构造解释，曾公开发布
ev("CLAIM_PROPOSED", "archive_claim", "claim-D",
   {"proposer_id": "s001", "source_refs": ["obs-inspect-01", "obs-photo-01"],
    "statement": "鼓楼北侧台基保留有近代排水沟构造。",
    "proposed_at": T.format(t="08-22T10:00:00+08:00")},
   T.format(t="08-22T10:00:00+08:00"), key="claim:D")
ev("CLAIM_REVIEWED", "review", "review-D",
   {"claim_id": "claim-D", "reviewer_id": "r001", "decision": "accepted",
    "source_refs": ["obs-inspect-01"],
    "rationale": "构造层位清晰，可发布。",
    "reviewed_at": T.format(t="08-25T14:00:00+08:00")},
   T.format(t="08-25T14:00:00+08:00"), key="review:D")

# 9. 公开范围（受同意范围约束）
ev("ARCHIVE_RELEASED", "archive_claim", "claim-B",
   {"claim_id": "claim-B", "scope": ["campus_research"],
    "released_at": T.format(t="09-01T09:00:00+08:00")},
   T.format(t="09-01T09:00:00+08:00"), key="release:B:campus")
ev("ARCHIVE_RELEASED", "archive_claim", "claim-C",
   {"claim_id": "claim-C", "scope": ["campus_research"],
    "released_at": T.format(t="09-01T09:05:00+08:00")},
   T.format(t="09-01T09:05:00+08:00"), key="release:C:campus")
ev("ARCHIVE_RELEASED", "archive_claim", "claim-D",
   {"claim_id": "claim-D", "scope": ["campus_research", "public_archive"],
    "released_at": T.format(t="09-02T09:00:00+08:00")},
   T.format(t="09-02T09:00:00+08:00"), key="release:D:all")

# 10. claim-B 已用于研究
ev("RESEARCH_USE_RECORDED", "archive_claim", "claim-B",
   {"claim_id": "claim-B", "study_id": "study-2026-gulou-07",
    "used_at": T.format(t="09-03T11:00:00+08:00")},
   T.format(t="09-03T11:00:00+08:00"), key="research-use:B")
# claim-D 的构造结论也已用于内部研究报告
ev("RESEARCH_USE_RECORDED", "archive_claim", "claim-D",
   {"claim_id": "claim-D", "study_id": "study-2026-gulou-07",
    "used_at": T.format(t="09-05T11:00:00+08:00")},
   T.format(t="09-05T11:00:00+08:00"), key="research-use:D")

# 11. 陈师傅撤回公开许可 → 沿引用链级联
ev("CONSENT_WITHDRAWN", "consent", "consent-p2",
   {"participant_id": "p2-chen", "effective_at": T.format(t="09-10T00:00:00+08:00"),
    "affected_scope": ["public_archive"],
    "note": "电话确认，不希望影像继续公开展示"},
   T.format(t="09-10T09:00:00+08:00"), key="consent:p2:withdraw-public")

# 12. 批量转档：4 个分片完成 0、1 后崩溃
ev("TRANSCODE_JOB_STARTED", "transcode_job", "job-t1",
   {"job_id": "job-t1", "shard_count": 4,
    "source_hashes": ["sha256:1111111111111111", "sha256:2222222222222222",
                      "sha256:3333333333333333"]},
   T.format(t="09-15T08:00:00+08:00"), key="job:t1")
ev("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job-t1",
   {"job_id": "job-t1", "shard_index": 0, "output_hash": "sha256:aa00"},
   T.format(t="09-15T08:10:00+08:00"), key="job:t1:shard:0")
ev("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job-t1",
   {"job_id": "job-t1", "shard_index": 1, "output_hash": "sha256:aa01"},
   T.format(t="09-15T08:20:00+08:00"), key="job:t1:shard:1")

# 13. 离线补传：同一事件重放
stored_photo = next(e for e in gate.events if e.get("idempotency_key") == "dev-s002:obs-photo-01")
dup = dict(stored_photo)
dup["event_id"] = "evt-device-reupload-dup"
r1 = gate.ingest(dup)
assert r1.status == "duplicate", r1

conflict = dict(stored_photo)
conflict["event_id"] = "evt-device-reupload-conflict"
conflict["payload"] = {**stored_photo["payload"], "consent_version": "consent-v2-2026"}
r2 = gate.ingest(conflict)
assert r2.status == "disputed" and r2.dispute_id, r2

(ROOT / "data" / "ledger.json").write_text(
    json.dumps(gate.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

# 供 README 演示的独立补传批次：一条完全重复 + 一条同键异文（文件哈希不同）
stored_inspect = next(e for e in gate.events if e.get("idempotency_key") == "dev-s002:obs-inspect-01")
batch = [
    {
        **stored_inspect,
        "event_id": "evt-reupload-inspect-dup",
    },
    {
        **stored_inspect,
        "event_id": "evt-reupload-inspect-conflict",
        "payload": {**stored_inspect["payload"], "file_hash": "sha256:deadbeefdeadbeef"},
    },
]
(ROOT / "data" / "offline_batch.json").write_text(
    json.dumps(batch, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

# 单事件校验样例同步到新契约
single = {
    "event_id": "092507-003-sample-001",
    "event_type": "COLLECTOR_QUALIFIED",
    "aggregate_type": "training_qualification",
    "aggregate_id": "qual-sample",
    "occurred_at": "2026-09-24T12:00:00+08:00",
    "version": 1,
    "idempotency_key": "sample:collector-qualified",
    "payload": {"collector_id": "sample-s001", "qualified_at": "2026-09-24T12:00:00+08:00"},
}
(ROOT / "data" / "sample.json").write_text(
    json.dumps(single, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)

print(f"ledger events: {len(gate.events)} (derived + dispute included)")
print("reupload duplicate:", r1.status)
print("reupload conflict:", r2.status, r2.dispute_id)
