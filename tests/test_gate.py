"""入库关业务规则测试。

使用内存 Gate 直接构造事件，覆盖：
- 十类资料分层的前置条件；
- 稳定事件键的幂等与同键异文争议；
- 观察不可变、解释引用不覆盖；
- 地点合并只迁移有效关系；
- 同意撤回沿引用链的级联；
- 复核回避；
- 转码断点续跑；
- 审计回溯（含所有被否决解释）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from heritage_field_intake.gate import (  # noqa: E402
    Gate,
    GateError,
    SCOPE_CAMPUS,
    SCOPE_PUBLIC,
    content_fingerprint,
)


def _event(
    etype,
    aggregate_type,
    aggregate_id,
    payload,
    occurred_at="2026-05-01T10:00:00+08:00",
    key=None,
    version=1,
):
    item = {
        "event_id": f"evt:{aggregate_id}:{etype}:{len(key or aggregate_id)}",
        "event_type": etype,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "occurred_at": occurred_at,
        "version": version,
        "payload": payload,
    }
    if key:
        item["idempotency_key"] = key
    return item


class GateFixture:
    """搭建：1 名采集者 s1、复核人 r1、地点 p、受访者 u（同意校内+公开）、计划、文件、观察。"""

    def __init__(self):
        self.gate = Gate()
        self.ts = lambda day: f"2026-05-{day:02d}T10:00:00+08:00"
        self.qualify("s1", self.ts(1))
        self.qualify("r1", self.ts(1))
        self.register_place("p", "鼓楼")
        self.grant("u", "v1", [SCOPE_CAMPUS, SCOPE_PUBLIC], self.ts(2))
        self.plan("plan1", ["p"], ["s1"], self.ts(3))
        self.raw_file("h1", 1024, self.ts(4))
        self.observation("obs1", "s1", "plan1", "p", "u", "h1", "v1", self.ts(4))

    def put(self, event, expected="accepted"):
        result = self.gate.ingest(event)
        assert result.status == expected, (event["event_type"], result.status, result.issues)
        return result

    def qualify(self, cid, at):
        return self.put(_event("COLLECTOR_QUALIFIED", "training_qualification", f"q-{cid}",
                               {"collector_id": cid, "qualified_at": at}, at, key=f"q:{cid}"))

    def register_place(self, pid, name):
        return self.put(_event("PLACE_REGISTERED", "place", pid, {"canonical_name": name},
                               self.ts(2), key=f"place:{pid}"))

    def grant(self, uid, ver, scopes, at):
        return self.put(_event("CONSENT_GRANTED", "consent", f"c-{uid}",
                               {"participant_id": uid, "consent_version": ver, "scope": scopes,
                                "granted_at": at}, at, key=f"grant:{uid}:{ver}"))

    def withdraw(self, uid, scopes, at, expected="accepted"):
        return self.put(_event("CONSENT_WITHDRAWN", "consent", f"c-{uid}",
                               {"participant_id": uid, "effective_at": at, "affected_scope": scopes},
                               at, key=f"withdraw:{uid}:{at}"), expected)

    def plan(self, plan_id, places, collectors, at, expected="accepted"):
        return self.put(_event("COLLECTION_PLAN_APPROVED", "collection_plan", plan_id,
                               {"plan_id": plan_id, "place_ids": places, "collector_ids": collectors},
                               at, key=f"plan:{plan_id}"), expected)

    def raw_file(self, h, size, at):
        return self.put(_event("RAW_FILE_RECEIVED", "source_file", f"f-{h}",
                               {"file_hash": h, "file_size": size, "captured_at": at},
                               at, key=f"file:{h}"))

    def observation(self, oid, collector, plan_id, place, user, h, ver, at, key=None, expected="accepted"):
        return self.put(_event("OBSERVATION_UPLOADED", "source_observation", oid,
                               {"collector_id": collector, "plan_id": plan_id, "place_id": place,
                                "participant_id": user, "file_hash": h, "consent_version": ver,
                                "captured_at": at}, at, key=key or f"obs:{oid}"), expected)

    def propose(self, cid, proposer, refs, statement="说法", at=None, expected="accepted"):
        at = at or self.ts(10)
        return self.put(_event("CLAIM_PROPOSED", "archive_claim", cid,
                               {"proposer_id": proposer, "source_refs": refs, "statement": statement},
                               at, key=f"claim:{cid}"), expected)

    def review(self, rid, cid, reviewer, decision, refs, at=None, expected="accepted"):
        at = at or self.ts(11)
        return self.put(_event("CLAIM_REVIEWED", "review", rid,
                               {"claim_id": cid, "reviewer_id": reviewer, "decision": decision,
                                "source_refs": refs}, at, key=f"review:{rid}"), expected)

    def release(self, cid, scopes, at, expected="accepted"):
        return self.put(_event("ARCHIVE_RELEASED", "archive_claim", cid,
                               {"claim_id": cid, "scope": scopes, "released_at": at},
                               at, key=f"release:{cid}:{at}"), expected)

    def research_use(self, cid, study, at):
        return self.put(_event("RESEARCH_USE_RECORDED", "archive_claim", cid,
                               {"claim_id": cid, "study_id": study, "used_at": at},
                               at, key=f"use:{cid}:{study}"))


class IntakeRuleTests(unittest.TestCase):
    def setUp(self):
        self.fx = GateFixture()
        self.gate = self.fx.gate

    # ---- 分层与前置条件 ---------------------------------------------------

    def test_unqualified_collector_cannot_plan_or_upload(self):
        self.fx.plan("plan2", ["p"], ["nobody"], self.fx.ts(3), expected="rejected")
        result = self.gate.ingest(_event(
            "OBSERVATION_UPLOADED", "source_observation", "obs-x",
            {"collector_id": "nobody", "plan_id": "plan1", "place_id": "p", "participant_id": "u",
             "file_hash": "h1", "consent_version": "v1", "captured_at": self.fx.ts(4)},
            key="obs:x"))
        self.assertEqual(result.status, "rejected")
        self.assertIn("collector_not_qualified", [i.code for i in result.issues])

    def test_plan_rejects_unregistered_or_merged_place(self):
        self.fx.plan("plan2", ["ghost"], ["s1"], self.fx.ts(3), expected="rejected")
        self.fx.register_place("p2", "古楼")
        self.gate.ingest(_event("PLACES_MERGED", "place", "p",
                                {"surviving_place_id": "p", "absorbed_place_ids": ["p2"]},
                                self.fx.ts(5), key="merge:1"))
        self.fx.plan("plan3", ["p2"], ["s1"], self.fx.ts(6), expected="rejected")

    def test_consent_scope_is_validated(self):
        bad = _event("CONSENT_GRANTED", "consent", "c-bad",
                     {"participant_id": "z", "consent_version": "v1", "scope": ["internet"],
                      "granted_at": self.fx.ts(2)}, key="grant:z")
        self.assertEqual(self.gate.ingest(bad).status, "rejected")

    def test_raw_file_required_before_observation_and_hash_unique(self):
        self.fx.raw_file("h2", 10, self.fx.ts(4))
        result = self.gate.ingest(_event(
            "RAW_FILE_RECEIVED", "source_file", "dup",
            {"file_hash": "h2", "file_size": 10, "captured_at": self.fx.ts(4)}, key="file:dup"))
        self.assertEqual(result.status, "rejected")

    def test_withdraw_requires_granted_scope(self):
        self.fx.withdraw("u", [SCOPE_PUBLIC], self.fx.ts(8))
        # 已撤回的范围不能再次撤回
        self.fx.withdraw("u", [SCOPE_PUBLIC], self.fx.ts(9), expected="rejected")
        self.fx.withdraw("never", [SCOPE_PUBLIC], self.fx.ts(9), expected="rejected")

    # ---- 幂等与争议 -------------------------------------------------------

    def test_identical_redelivery_is_idempotent(self):
        stored = next(e for e in self.gate.events if e["idempotency_key"] == "obs:obs1")
        count_before = len(self.gate.events)
        again = {**stored, "event_id": "evt-device-redelivery"}
        result = self.gate.ingest(again)
        self.assertEqual(result.status, "duplicate")
        self.assertEqual(result.event_id, stored["event_id"])
        self.assertEqual(len(self.gate.events), count_before)
        self.assertEqual(self.gate.events.count(stored), 1)

    def test_same_key_different_content_opens_dispute_once(self):
        stored = next(e for e in self.gate.events if e["idempotency_key"] == "obs:obs1")
        conflict = {**stored, "event_id": "evt-b",
                    "payload": {**stored["payload"], "file_hash": "hX"}}
        r1 = self.gate.ingest(conflict)
        r2 = self.gate.ingest(conflict)
        self.assertEqual(r1.status, "disputed")
        self.assertEqual(r2.status, "disputed")
        self.assertEqual(r1.dispute_id, r2.dispute_id)
        open_disputes = self.gate.open_disputes()
        self.assertEqual(len(open_disputes), 1)
        self.assertEqual(open_disputes[0]["opened"]["payload"]["reason"], "same_key_file_differs")
        # 冲突内容没有进入投影
        self.assertNotIn("hX", self.gate.files)

    def test_consent_scope_conflict_is_classified(self):
        stored = next(e for e in self.gate.events if e["idempotency_key"] == "obs:obs1")
        conflict = {**stored, "event_id": "evt-b",
                    "payload": {**stored["payload"], "consent_version": "v2"}}
        result = self.gate.ingest(conflict)
        self.assertEqual(result.status, "disputed")
        self.assertEqual(self.gate.open_disputes()[0]["opened"]["payload"]["reason"],
                         "same_key_consent_version_differs")

    def test_fingerprint_excludes_envelope_event_id(self):
        a = {"event_id": "x", "event_type": "T", "payload": {"h": 1}}
        b = {"event_id": "y", "event_type": "T", "payload": {"h": 1}}
        self.assertEqual(content_fingerprint(a), content_fingerprint(b))

    def test_dispute_can_be_resolved_once(self):
        stored = next(e for e in self.gate.events if e["idempotency_key"] == "obs:obs1")
        conflict = {**stored, "event_id": "evt-b",
                    "payload": {**stored["payload"], "file_hash": "hX"}}
        opened = self.gate.ingest(conflict)
        dispute_id = opened.dispute_id
        resolve = _event("DISPUTE_RESOLVED", "dispute", dispute_id,
                         {"dispute_id": dispute_id, "resolution": "manual",
                          "kept_event_id": stored["event_id"]},
                         self.fx.ts(20), key=f"resolve:{dispute_id}")
        self.assertEqual(self.gate.ingest(resolve).status, "accepted")
        # 已结案不能二次结案（另一设备的结案指令）
        resolve_again = {**resolve, "event_id": "evt-resolve-2",
                         "idempotency_key": f"resolve2:{dispute_id}"}
        self.assertEqual(self.gate.ingest(resolve_again).status, "rejected")
        self.assertEqual(self.gate.open_disputes(), [])

    # ---- 观察不可变、解释引用 ---------------------------------------------

    def test_observation_cannot_be_overwritten(self):
        stored = next(e for e in self.gate.events if e["idempotency_key"] == "obs:obs1")
        changed = {**stored, "payload": {**stored["payload"], "transcript_excerpt": "改写"}}
        # 同键异文进争议而非覆盖；不同键同聚合再传则被前置条件拒绝
        self.assertEqual(self.gate.ingest(changed).status, "disputed")
        other = {**stored, "event_id": "new", "idempotency_key": "obs:obs1-v2"}
        result = self.gate.ingest(other)
        self.assertEqual(result.status, "rejected")
        self.assertIn("observation_exists", [i.code for i in result.issues])
        # 原始观察内容未被改动
        self.assertNotIn("transcript_excerpt",
                         self.gate.observations["obs1"]["event"]["payload"])

    def test_claim_must_reference_existing_observation_or_claim(self):
        self.fx.propose("c1", "s1", ["obs1"])
        result = self.gate.ingest(_event(
            "CLAIM_PROPOSED", "archive_claim", "c-bad",
            {"proposer_id": "s1", "source_refs": ["ghost"], "statement": "无据"}, key="claim:bad"))
        self.assertEqual(result.status, "rejected")
        # 后来的解释可以引用旧解释+旧观察
        self.fx.propose("c2", "s1", ["obs1", "c1"])
        self.assertEqual(self.gate.source_closure_ids("c2"), ["obs1"])

    def test_correction_does_not_mutate_observation(self):
        self.gate.ingest(_event("CORRECTION_FILED", "correction", "corr-1",
                                {"target_ref": "obs1", "reason": "转录错字"},
                                self.fx.ts(12), key="corr:1"))
        self.assertEqual(len(self.gate.observations["obs1"].get("corrections", [])), 1)
        self.assertNotIn("错字", json.dumps(self.gate.observations["obs1"]["event"], ensure_ascii=False))

    # ---- 复核 -------------------------------------------------------------

    def test_reviewer_cannot_review_own_claim_or_own_material(self):
        self.fx.propose("c1", "s1", ["obs1"])
        result = self.gate.ingest(_event(
            "CLAIM_REVIEWED", "review", "r-self",
            {"claim_id": "c1", "reviewer_id": "s1", "decision": "accepted",
             "source_refs": ["obs1"]}, key="review:self"))
        self.assertEqual(result.status, "rejected")
        self.assertTrue(any(i.code == "self_review" for i in result.issues))
        # r1 与材料无关，可复核
        self.fx.review("r1-ok", "c1", "r1", "accepted", ["obs1"])

    def test_only_accepted_claim_can_release_within_consent(self):
        self.fx.propose("c1", "s1", ["obs1"])
        self.fx.release("c1", [SCOPE_CAMPUS], self.fx.ts(12), expected="rejected")  # 尚未复核
        self.fx.review("rv1", "c1", "r1", "rejected", ["obs1"])
        self.fx.release("c1", [SCOPE_CAMPUS], self.fx.ts(12), expected="rejected")  # 已否决

    def test_release_blocked_beyond_consent_scope(self):
        # 新受访者只同意校内
        self.fx.grant("u2", "v1", [SCOPE_CAMPUS], self.fx.ts(2))
        self.fx.register_place("p3", "东巷")
        self.fx.plan("plan2", ["p3"], ["s1"], self.fx.ts(3))
        self.fx.raw_file("h3", 9, self.fx.ts(4))
        self.fx.observation("obs3", "s1", "plan2", "p3", "u2", "h3", "v1", self.fx.ts(4))
        self.fx.propose("c3", "s1", ["obs3"])
        self.fx.review("rv3", "c3", "r1", "accepted", ["obs3"])
        result = self.fx.release("c3", [SCOPE_PUBLIC], self.fx.ts(12), expected="rejected")
        self.assertIn("scope_not_granted", [i.code for i in result.issues])
        # 校内范围在授权内，可以发布
        self.fx.release("c3", [SCOPE_CAMPUS], self.fx.ts(12))

    # ---- 地点合并 ---------------------------------------------------------

    def test_merge_migrates_only_effective_relations(self):
        self.fx.register_place("p2", "古楼")
        self.fx.grant("u3", "v1", [SCOPE_CAMPUS, SCOPE_PUBLIC], self.fx.ts(2))
        self.fx.plan("plan2", ["p2"], ["s1"], self.fx.ts(3))
        self.fx.raw_file("h-ok", 9, self.fx.ts(4))
        self.fx.raw_file("h-dead", 9, self.fx.ts(4))
        self.fx.observation("obs-ok", "s1", "plan2", "p2", "u3", "h-ok", "v1", self.fx.ts(4))
        self.fx.observation("obs-dead", "s1", "plan2", "p2", "u", "h-dead", "v1", self.fx.ts(4))
        # u 在合并前撤回全部范围；u3 仍有效
        self.fx.withdraw("u", [SCOPE_CAMPUS, SCOPE_PUBLIC], self.fx.ts(5))
        merge = _event("PLACES_MERGED", "place", "p",
                       {"surviving_place_id": "p", "absorbed_place_ids": ["p2"]},
                       self.fx.ts(6), key="merge:1")
        self.gate.ingest(merge)
        survivor = self.gate.places["p"]
        self.assertIn("obs-ok", survivor["migrated_observations"])
        self.assertNotIn("obs-dead", survivor["migrated_observations"])
        rejected = self.gate.places["p2"]["rejected_relations"]
        self.assertEqual([r["observation_id"] for r in rejected], ["obs-dead"])
        self.assertFalse(self.gate.places["p2"]["active"])
        self.assertEqual(self.gate.observations["obs-ok"]["migrated_to"], "p")
        self.assertIsNone(self.gate.observations["obs-dead"]["migrated_to"])

    # ---- 同意撤回级联 -----------------------------------------------------

    def test_withdraw_restricts_unreleased_and_annotates_released(self):
        self.fx.propose("c-unpub", "s1", ["obs1"])  # 未发布
        self.fx.propose("c-pub", "s1", ["obs1"])
        self.fx.review("rv-u", "c-unpub", "r1", "accepted", ["obs1"])
        self.fx.review("rv-p", "c-pub", "r1", "accepted", ["obs1"])
        self.fx.release("c-pub", [SCOPE_PUBLIC], self.fx.ts(12))
        before = len(self.gate.events)
        self.fx.withdraw("u", [SCOPE_PUBLIC], self.fx.ts(13))
        # 撤回事件本身 + 派生事件
        derived = self.gate.events[before + 1:]
        kinds = sorted(e["event_type"] for e in derived)
        self.assertEqual(kinds, ["ARCHIVE_ANNOTATED", "RELEASE_RESTRICTED", "RELEASE_RESTRICTED"])
        # 未发布档案此后不能发布到公开范围
        result = self.fx.release("c-unpub", [SCOPE_PUBLIC], self.fx.ts(14), expected="rejected")
        self.assertTrue(any(i.code == "claim_restricted" for i in result.issues))
        # 已发布版本状态为受限但保留发布记录与加注
        state = self.gate.claims["c-pub"]
        self.assertEqual(state.status, "restricted")
        self.assertIn(SCOPE_PUBLIC, state.released_scopes)
        self.assertEqual(len(state.annotations), 1)

    def test_withdraw_for_research_used_version_appends_note_and_keeps_record(self):
        self.fx.propose("c1", "s1", ["obs1"])
        self.fx.review("rv1", "c1", "r1", "accepted", ["obs1"])
        self.fx.release("c1", [SCOPE_CAMPUS], self.fx.ts(12))
        self.fx.research_use("c1", "study-9", self.fx.ts(13))
        self.fx.withdraw("u", [SCOPE_CAMPUS], self.fx.ts(14))
        state = self.gate.claims["c1"]
        self.assertEqual(len(state.annotations), 1)
        self.assertIn("已用于研究", state.annotations[0]["payload"]["note"])
        # 研究使用记录与发布记录均保留
        self.assertEqual(len(state.research_uses), 1)
        self.assertEqual(len(state.releases), 1)

    def test_withdraw_propagates_through_claim_reference_chain(self):
        self.fx.propose("c-base", "s1", ["obs1"])
        # c2 只引用解释；撤回仍应顺链到达 obs1
        self.fx.propose("c-top", "s1", ["c-base"])
        self.fx.withdraw("u", [SCOPE_PUBLIC], self.fx.ts(9))
        restricted = {e["aggregate_id"] for e in self.gate.events
                      if e["event_type"] == "RELEASE_RESTRICTED"}
        self.assertEqual(restricted, {"c-base", "c-top"})

    # ---- 转码断点续跑 -----------------------------------------------------

    def test_transcode_resumes_from_first_unfinished_shard(self):
        start = _event("TRANSCODE_JOB_STARTED", "transcode_job", "job1",
                       {"job_id": "job1", "shard_count": 4}, self.fx.ts(20), key="job:1")
        self.gate.ingest(start)
        for i, out in ((0, "a0"), (1, "a1")):
            self.gate.ingest(_event("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job1",
                                    {"job_id": "job1", "shard_index": i, "output_hash": out},
                                    self.fx.ts(20 + i), key=f"job1:{i}"))
        self.assertEqual(self.gate.next_shard("job1"), 2)
        self.assertEqual(self.gate.pending_shards("job1"), [2, 3])
        # 模拟崩溃后补传同一分片：相同哈希幂等，不同哈希拒绝
        dup = _event("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job1",
                     {"job_id": "job1", "shard_index": 1, "output_hash": "a1"},
                     self.fx.ts(22), key="job1:1-redeliver")
        # 不同 key 的同分片同哈希：投影 setdefault 不覆盖，前置不拦（内容一致，视为重放完成）
        r = self.gate.ingest(dup)
        self.assertEqual(r.status, "accepted")
        clash = _event("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job1",
                       {"job_id": "job1", "shard_index": 1, "output_hash": "different"},
                       self.fx.ts(22), key="job1:1-clash")
        self.assertEqual(self.gate.ingest(clash).status, "rejected")
        # 越界分片
        bad = _event("TRANSCODE_SHARD_COMPLETED", "transcode_job", "job1",
                     {"job_id": "job1", "shard_index": 4, "output_hash": "x"},
                     self.fx.ts(22), key="job1:4")
        self.assertEqual(self.gate.ingest(bad).status, "rejected")

    def test_shard_completion_requires_started_job(self):
        r = self.gate.ingest(_event("TRANSCODE_SHARD_COMPLETED", "transcode_job", "jobX",
                                    {"job_id": "jobX", "shard_index": 0, "output_hash": "x"},
                                    key="x"))
        self.assertEqual(r.status, "rejected")


class LedgerPersistenceTests(unittest.TestCase):
    def test_replay_rebuilds_projection_and_roundtrip(self):
        fx = GateFixture()
        fx.propose("c1", "s1", ["obs1"])
        fx.review("rv1", "c1", "r1", "accepted", ["obs1"])
        blob = json.dumps(fx.gate.events, ensure_ascii=False)
        rebuilt = Gate.replay(json.loads(blob))
        self.assertEqual([e["event_id"] for e in rebuilt.events],
                         [e["event_id"] for e in fx.gate.events])
        self.assertEqual(rebuilt.source_closure_ids("c1"), ["obs1"])
        self.assertEqual(rebuilt.claims["c1"].status, "accepted")
        # 重放账本后重复投递仍然幂等
        stored = next(e for e in rebuilt.events if e["idempotency_key"] == "obs:obs1")
        self.assertEqual(rebuilt.ingest({**stored, "event_id": "z"}).status, "duplicate")

    def test_ledger_file_must_be_array(self):
        path = ROOT / "data" / "sample.json"
        with self.assertRaises(GateError):
            Gate.from_file(path)


class ContractSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text("utf-8"))
        self.sample = json.loads((ROOT / "data" / "sample.json").read_text("utf-8"))

    def test_sample_validates(self):
        from heritage_field_intake.contracts import validate_event
        self.assertEqual(validate_event(self.sample, self.schema), [])

    def test_all_gate_event_types_are_registered(self):
        fx = GateFixture()
        fx.propose("c1", "s1", ["obs1"])
        fx.review("rv1", "c1", "r1", "accepted", ["obs1"])
        fx.release("c1", [SCOPE_CAMPUS], fx.ts(12))
        fx.research_use("c1", "s9", fx.ts(13))
        fx.withdraw("u", [SCOPE_CAMPUS], fx.ts(14))
        fx.gate.ingest(_event("CORRECTION_FILED", "correction", "x",
                              {"target_ref": "obs1", "reason": "r"}, key="cc"))
        from heritage_field_intake.contracts import validate_event
        for event in fx.gate.events:
            issues = validate_event(event, self.schema)
            self.assertEqual(issues, [], f"{event['event_type']}: {issues}")


class SampleLedgerTests(unittest.TestCase):
    """仓库内置样例账本必须能重放并完整审计。"""

    @classmethod
    def setUpClass(cls):
        schema = json.loads((ROOT / "contracts" / "domain.schema.json").read_text("utf-8"))
        cls.gate = Gate.from_file(ROOT / "data" / "ledger.json", schema)

    def test_sample_scenario_state(self):
        g = self.gate
        # 异名合并：别名挂在规范地点，被合并地点失活
        self.assertIn("谯楼", g.places["place-gulou"]["aliases"])
        self.assertFalse(g.places["place-gulou-old"]["active"])
        # 陈师傅公开范围已撤回，校内仍有效
        self.assertEqual(g.effective_scopes("p2-chen", None), {SCOPE_CAMPUS})
        # 转档崩溃现场：分片 0、1 完成，2、3 待续
        self.assertEqual(g.pending_shards("job-t1"), [2, 3])
        # claim-A（照片晚于修缮）被否决且未发布
        self.assertEqual(g.claims["claim-A"].status, "restricted")
        self.assertEqual(g.claims["claim-A"].reviews[-1]["payload"]["decision"], "rejected")
        # 存在一条未决争议
        self.assertEqual(len(g.open_disputes()), 1)

    def test_audit_returns_full_chain_for_public_narrative(self):
        from heritage_field_intake.audit import build_audit
        report = build_audit(self.gate, "claim-D")
        narrative = report["public_narrative"]
        self.assertIn("排水沟", narrative["statement"])
        source_ids = {s["observation"]["aggregate_id"] for s in report["sources"]}
        self.assertEqual(source_ids, {"obs-inspect-01", "obs-photo-01"})
        # 授权链：授予+撤回都在
        chen = report["consents"]["p2-chen"]
        self.assertEqual(len(chen["grants"]), 1)
        self.assertEqual(len(chen["withdrawals"]), 1)
        self.assertEqual(chen["current_scopes"], [SCOPE_CAMPUS])
        # 专业复核与被否决解释（claim-A 共用 obs-photo-01）
        self.assertEqual([r["payload"]["decision"] for r in report["reviews"]], ["accepted"])
        self.assertEqual([c["claim_id"] for c in report["rejected_claims"]], ["claim-A"])
        # 已发布且用于研究 → 撤回后加注说明
        self.assertTrue(any("已用于研究" in a["payload"]["note"] for a in report["annotations"]))
        # 地点沿革含合并链
        self.assertIn("place-gulou-old", report["place_history"])
        # 原始文件校验信息可回溯
        hashes = {f["payload"]["file_hash"] for f in report["raw_files"]}
        self.assertIn("sha256:2222222222222222", hashes)

    def test_audit_unknown_claim_raises(self):
        from heritage_field_intake.audit import build_audit
        with self.assertRaises(KeyError):
            build_audit(self.gate, "nope")


if __name__ == "__main__":
    unittest.main()
