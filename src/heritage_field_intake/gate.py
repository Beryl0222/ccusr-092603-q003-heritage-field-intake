"""入库关业务层。

事件流是唯一事实来源：Gate 重放已入库事件建立投影，ingest 负责契约校验、
稳定事件键幂等、争议队列与领域前置条件。观察（source_observation）一经入库
不可修改；后来的解释只能引用，不能覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import ContractIssue, validate_event

# 同意/公开范围：校内研究 ⊂ 公开档案
SCOPE_CAMPUS = "campus_research"
SCOPE_PUBLIC = "public_archive"
ALL_SCOPES = (SCOPE_CAMPUS, SCOPE_PUBLIC)

REVIEW_ACCEPTED = "accepted"
REVIEW_REJECTED = "rejected"


class GateError(ValueError):
    """输入无法构造成事件（非契约问题）。"""


@dataclass(frozen=True)
class GateIssue:
    code: str
    message: str
    field: str = ""


@dataclass
class IngestionResult:
    status: str  # accepted | duplicate | disputed | rejected
    event_id: str | None = None
    issues: list[GateIssue] = field(default_factory=list)
    dispute_id: str | None = None
    derived: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "duplicate")


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def content_fingerprint(event: Mapping[str, Any]) -> str:
    """同一稳定键下用于判重的内容指纹。

    event_id 是设备信封标识，补传时允许不同，故排除；其余字段（含载荷中的
    文件哈希、同意范围）任一不同即视为同键异文。
    """
    body = {k: v for k, v in event.items() if k != "event_id"}
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class _ClaimState:
    event: Mapping[str, Any]
    reviews: list[Mapping[str, Any]] = field(default_factory=list)
    corrections: list[Mapping[str, Any]] = field(default_factory=list)
    releases: list[Mapping[str, Any]] = field(default_factory=list)
    research_uses: list[Mapping[str, Any]] = field(default_factory=list)
    annotations: list[Mapping[str, Any]] = field(default_factory=list)
    restrictions: list[Mapping[str, Any]] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.restrictions:
            return "restricted"
        if self.releases:
            return "released"
        if self.reviews:
            return self.reviews[-1]["payload"]["decision"]
        return "proposed"

    @property
    def released_scopes(self) -> set[str]:
        scopes: set[str] = set()
        for release in self.releases:
            scopes.update(release["payload"].get("scope", []))
        return scopes

    @property
    def used_in_research(self) -> bool:
        return bool(self.research_uses)


class Gate:
    """从事件流重建的入库关投影与写入门禁。"""

    def __init__(self, schema: Mapping[str, Any] | None = None) -> None:
        self.schema = schema
        self.events: list[Mapping[str, Any]] = []
        self._keys: dict[str, str] = {}  # idempotency_key -> event_id
        self._fingerprints: dict[str, str] = {}
        self.qualifications: dict[str, Mapping[str, Any]] = {}
        self.plans: dict[str, Mapping[str, Any]] = {}
        self.places: dict[str, dict[str, Any]] = {}
        self.consents: dict[str, dict[str, Any]] = {}
        self.files: dict[str, Mapping[str, Any]] = {}
        self.observations: dict[str, dict[str, Any]] = {}
        self.claims: dict[str, _ClaimState] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.disputes: dict[str, list[dict[str, Any]]] = {}

    # ---- 装载与重放 -------------------------------------------------------

    @classmethod
    def replay(cls, events: Sequence[Mapping[str, Any]], schema: Mapping[str, Any] | None = None) -> "Gate":
        gate = cls(schema)
        for event in events:
            gate.events.append(event)
            gate._register_key(event)
            gate._apply(event, cascade=False)
        return gate

    @classmethod
    def from_file(cls, path: str | Path, schema: Mapping[str, Any] | None = None) -> "Gate":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise GateError("账本必须是事件数组")
        return cls.replay(raw, schema)

    # ---- 写入 -------------------------------------------------------------

    def ingest(self, event: Mapping[str, Any]) -> IngestionResult:
        if not isinstance(event, Mapping):
            return IngestionResult("rejected", issues=[GateIssue("object_required", "事件必须是 JSON 对象", "$")])

        if self.schema is not None:
            contract_issues = validate_event(event, self.schema)
            if contract_issues:
                return IngestionResult(
                    "rejected",
                    event_id=event.get("event_id") if isinstance(event.get("event_id"), str) else None,
                    issues=[GateIssue(i.code, i.message, i.field) for i in contract_issues],
                )

        key = event.get("idempotency_key")
        if isinstance(key, str) and key in self._keys:
            stored_id = self._keys[key]
            if self._fingerprints.get(key) == content_fingerprint(event):
                return IngestionResult("duplicate", event_id=stored_id)
            existing = self.disputes.get(key, [])
            if existing and existing[-1]["status"] == "open":
                return IngestionResult("disputed", event_id=stored_id, dispute_id=existing[-1]["id"])
            return self._open_dispute(key, stored_id, event)

        issues = self._preconditions(event)
        if issues:
            return IngestionResult("rejected", event_id=event.get("event_id"), issues=issues)

        self._append(event)
        result = IngestionResult("accepted", event_id=event["event_id"])

        for derived in self._cascades(event):
            self._append(derived)
            result.derived.append(derived["event_id"])

        return result

    def _append(self, event: Mapping[str, Any]) -> None:
        self._register_key(event)
        self.events.append(event)
        self._apply(event, cascade=True)

    def _register_key(self, event: Mapping[str, Any]) -> None:
        key = event.get("idempotency_key")
        if isinstance(key, str):
            self._keys.setdefault(key, event["event_id"])
            self._fingerprints.setdefault(key, content_fingerprint(event))

    def _open_dispute(self, key: str, stored_id: str, received: Mapping[str, Any]) -> IngestionResult:
        dispute_id = f"dispute:{key}"
        stored = next(e for e in self.events if e["event_id"] == stored_id)
        reason = self._describe_difference(stored, received)
        opened = {
            "event_id": f"{dispute_id}:opened:{len(self.events) + 1:04d}",
            "event_type": "DISPUTE_OPENED",
            "aggregate_type": "dispute",
            "aggregate_id": dispute_id,
            "occurred_at": received["occurred_at"],
            "version": len(self.disputes.get(key, ())) + 1,
            "idempotency_key": f"system:dispute-opened:{key}",
            "payload": {
                "idempotency_key": key,
                "stored_event_id": stored_id,
                "received_event_id": received["event_id"],
                "reason": reason,
            },
        }
        self.events.append(opened)
        self._apply(opened, cascade=False)
        return IngestionResult("disputed", event_id=received["event_id"], dispute_id=dispute_id)

    @staticmethod
    def _describe_difference(stored: Mapping[str, Any], received: Mapping[str, Any]) -> str:
        if stored.get("payload", {}).get("file_hash") != received.get("payload", {}).get("file_hash"):
            return "same_key_file_differs"
        if stored.get("payload", {}).get("consent_version") != received.get("payload", {}).get("consent_version"):
            return "same_key_consent_version_differs"
        return "same_key_content_differs"

    # ---- 前置条件 ---------------------------------------------------------

    def _preconditions(self, event: Mapping[str, Any]) -> list[GateIssue]:
        etype = event["event_type"]
        payload = event["payload"]
        checker = getattr(self, f"_check_{etype.lower()}", None)
        issues = list(checker(event, payload)) if checker else []
        for ts_field in ("qualified_at", "granted_at", "captured_at", "released_at", "effective_at"):
            if ts_field in payload and parse_ts(payload[ts_field]) is None:
                issues.append(GateIssue("timezone_required", f"payload.{ts_field} 必须携带时区", f"payload.{ts_field}"))
        return issues

    def _check_collector_qualified(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        cid = p["collector_id"]
        if cid in self.qualifications:
            return [GateIssue("already_qualified", f"采集者 {cid} 已具备培训资格", "payload.collector_id")]
        return []

    def _check_collection_plan_approved(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        if p["plan_id"] in self.plans:
            issues.append(GateIssue("plan_exists", "采集计划已存在", "payload.plan_id"))
        at = parse_ts(e["occurred_at"])
        for cid in p["collector_ids"]:
            qual = self.qualifications.get(cid)
            if qual is None:
                issues.append(GateIssue("collector_not_qualified", f"采集者 {cid} 尚无培训资格", "payload.collector_ids"))
            elif at is not None and parse_ts(qual["payload"]["qualified_at"]) > at:
                issues.append(GateIssue("qualification_after_plan", f"采集者 {cid} 的资格晚于计划批准", "payload.collector_ids"))
        for place_id in p["place_ids"]:
            place = self.places.get(place_id)
            if place is None:
                issues.append(GateIssue("place_missing", f"地点 {place_id} 未登记", "payload.place_ids"))
            elif not place["active"]:
                issues.append(GateIssue("place_inactive", f"地点 {place_id} 已被合并", "payload.place_ids"))
        return issues

    def _check_place_registered(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if e["aggregate_id"] in self.places:
            return [GateIssue("place_exists", "地点已登记", "aggregate_id")]
        return []

    def _check_place_alias_recorded(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if e["aggregate_id"] not in self.places:
            return [GateIssue("place_missing", "别名必须挂在已登记地点上", "aggregate_id")]
        return []

    def _check_places_merged(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        survivor = self.places.get(p["surviving_place_id"])
        if survivor is None:
            issues.append(GateIssue("place_missing", "保留地点不存在", "payload.surviving_place_id"))
        elif not survivor["active"]:
            issues.append(GateIssue("place_inactive", "保留地点已被合并", "payload.surviving_place_id"))
        for place_id in p["absorbed_place_ids"]:
            place = self.places.get(place_id)
            if place is None:
                issues.append(GateIssue("place_missing", f"被合并地点 {place_id} 不存在", "payload.absorbed_place_ids"))
            elif not place["active"]:
                issues.append(GateIssue("place_inactive", f"地点 {place_id} 已被合并", "payload.absorbed_place_ids"))
            elif survivor is not None and place_id == survivor["id"]:
                issues.append(GateIssue("merge_self", "保留地点不能同时被合并", "payload.absorbed_place_ids"))
        return issues

    def _check_consent_granted(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        scopes = p.get("scope", [])
        if not isinstance(scopes, list) or not scopes or any(s not in ALL_SCOPES for s in scopes):
            issues.append(GateIssue("invalid_scope", "同意范围必须是校内研究/公开档案的非空列表", "payload.scope"))
        record = self.consents.get(p["participant_id"])
        if record:
            versions = [g["payload"]["consent_version"] for g in record["grants"]]
            if p["consent_version"] in versions:
                issues.append(GateIssue("consent_version_exists", "同意版本已存在", "payload.consent_version"))
        return issues

    def _check_consent_withdrawn(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        record = self.consents.get(p["participant_id"])
        at = parse_ts(p["effective_at"])
        if record is None or not record["grants"]:
            return [GateIssue("consent_missing", "受访者从未授权，无许可可撤回", "payload.participant_id")]
        scopes = p.get("affected_scope", [])
        if not isinstance(scopes, list) or not scopes or any(s not in ALL_SCOPES for s in scopes):
            return [GateIssue("invalid_scope", "撤回范围必须是校内研究/公开档案的非空列表", "payload.affected_scope")]
        effective = self.effective_scopes(p["participant_id"], at)
        extra = set(scopes) - effective
        if extra:
            return [GateIssue("scope_not_granted", f"以下范围当前未授权，不能撤回：{sorted(extra)}", "payload.affected_scope")]
        return []

    def _check_raw_file_received(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        if not isinstance(p.get("file_size"), int) or isinstance(p["file_size"], bool) or p["file_size"] <= 0:
            issues.append(GateIssue("positive_integer", "文件大小必须是正整数（字节）", "payload.file_size"))
        if p["file_hash"] in self.files:
            issues.append(GateIssue("file_exists", "该哈希的原始文件已入库", "payload.file_hash"))
        return issues

    def _check_observation_uploaded(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        obs_id = e["aggregate_id"]
        if obs_id in self.observations:
            issues.append(GateIssue("observation_exists", "观察记录已存在且不可覆盖", "aggregate_id"))
        if p["collector_id"] not in self.qualifications:
            issues.append(GateIssue("collector_not_qualified", "上传者无培训资格", "payload.collector_id"))
        plan = self.plans.get(p["plan_id"])
        if plan is None:
            issues.append(GateIssue("plan_missing", "采集计划不存在", "payload.plan_id"))
        else:
            if p["collector_id"] not in plan["payload"]["collector_ids"]:
                issues.append(GateIssue("collector_not_in_plan", "上传者不在该采集计划名单内", "payload.collector_id"))
            if p["place_id"] not in plan["payload"]["place_ids"]:
                issues.append(GateIssue("place_not_in_plan", "地点不在该采集计划内", "payload.place_id"))
        place = self.places.get(p["place_id"])
        if place is not None and not place["active"]:
            issues.append(GateIssue("place_inactive", "观察必须挂到合并后的保留地点", "payload.place_id"))
        if p["file_hash"] not in self.files:
            issues.append(GateIssue("raw_file_missing", "必须先完成原始文件校验入库", "payload.file_hash"))
        record = self.consents.get(p["participant_id"])
        if record is None or not any(
            g["payload"]["consent_version"] == p["consent_version"] for g in record["grants"]
        ):
            issues.append(GateIssue("consent_version_missing", "受访者同意版本不存在", "payload.consent_version"))
        else:
            at = parse_ts(p.get("captured_at")) or parse_ts(e["occurred_at"])
            if not self.effective_scopes(p["participant_id"], at):
                issues.append(GateIssue("consent_withdrawn", "采集时受访者授权已无有效范围", "payload.participant_id"))
        return issues

    def _check_claim_proposed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        claim_id = e["aggregate_id"]
        if claim_id in self.claims:
            issues.append(GateIssue("claim_exists", "解释主张已存在", "aggregate_id"))
        refs = p.get("source_refs", [])
        if not isinstance(refs, list) or not refs:
            issues.append(GateIssue("source_refs_required", "解释必须至少引用一条观察或旧解释", "payload.source_refs"))
        else:
            for ref in refs:
                if ref not in self.observations and ref not in self.claims:
                    issues.append(GateIssue("source_missing", f"引用 {ref} 不存在", "payload.source_refs"))
        if not isinstance(p.get("statement"), str) or not p["statement"].strip():
            issues.append(GateIssue("statement_required", "解释叙述不能为空", "payload.statement"))
        return issues

    def _check_claim_reviewed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        claim = self.claims.get(p["claim_id"])
        if claim is None:
            return [GateIssue("claim_missing", "被复核的解释不存在", "payload.claim_id")]
        if p["decision"] not in (REVIEW_ACCEPTED, REVIEW_REJECTED):
            issues.append(GateIssue("invalid_decision", "复核结论只能是 accepted/rejected", "payload.decision"))
        refs = p.get("source_refs", [])
        if not refs:
            issues.append(GateIssue("source_refs_required", "复核必须回连所依据的原始材料", "payload.source_refs"))
        reviewer = p["reviewer_id"]
        if reviewer == claim.event["payload"]["proposer_id"]:
            issues.append(GateIssue("self_review", "复核人不得复核自己提出的解释", "payload.reviewer_id"))
        for obs in self.source_closure(p["claim_id"]):
            if obs["event"]["payload"]["collector_id"] == reviewer:
                issues.append(GateIssue("self_review", "复核人不得复核自己采集的材料", "payload.reviewer_id"))
                break
        return issues

    def _check_correction_filed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if p["target_ref"] not in self.observations and p["target_ref"] not in self.claims:
            return [GateIssue("target_missing", "纠错对象不存在", "payload.target_ref")]
        if not isinstance(p.get("reason"), str) or not p["reason"].strip():
            return [GateIssue("reason_required", "纠错理由不能为空", "payload.reason")]
        return []

    def _check_archive_released(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        claim = self.claims.get(p["claim_id"])
        if claim is None:
            return [GateIssue("claim_missing", "公开对象不存在", "payload.claim_id")]
        scopes = p.get("scope", [])
        if not isinstance(scopes, list) or not scopes or any(s not in ALL_SCOPES for s in scopes):
            issues.append(GateIssue("invalid_scope", "公开范围非法", "payload.scope"))
        if not claim.reviews or claim.reviews[-1]["payload"]["decision"] != REVIEW_ACCEPTED:
            issues.append(GateIssue("claim_not_accepted", "只有复核通过的解释才能公开", "payload.claim_id"))
        blocked: set[str] = set()
        for restriction in claim.restrictions:
            blocked.update(restriction["payload"].get("restricted_scopes", []))
        if set(scopes) & blocked:
            issues.append(GateIssue("claim_restricted", "该档案的对应范围已被同意撤回限制", "payload.claim_id"))
        at = parse_ts(p.get("released_at")) or parse_ts(e["occurred_at"])
        participants = {obs["event"]["payload"]["participant_id"] for obs in self.source_closure(p["claim_id"])}
        for participant in participants:
            granted = self.effective_scopes(participant, at)
            missing = set(scopes) - granted
            if missing:
                issues.append(GateIssue("scope_not_granted", f"受访者 {participant} 未授权 {sorted(missing)}", "payload.scope"))
        already = claim.released_scopes
        if set(scopes) <= already:
            issues.append(GateIssue("already_released", "该范围已发布（补传请复用稳定事件键）", "payload.scope"))
        return issues

    def _check_research_use_recorded(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if p["claim_id"] not in self.claims:
            return [GateIssue("claim_missing", "研究使用对象不存在", "payload.claim_id")]
        return []

    def _check_archive_annotated(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if p["claim_id"] not in self.claims:
            return [GateIssue("claim_missing", "加注对象不存在", "payload.claim_id")]
        return []

    def _check_release_restricted(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        if p["claim_id"] not in self.claims:
            return [GateIssue("claim_missing", "限制对象不存在", "payload.claim_id")]
        return []

    def _check_transcode_job_started(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        issues: list[GateIssue] = []
        count = p.get("shard_count")
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            issues.append(GateIssue("positive_integer", "分片数必须是正整数", "payload.shard_count"))
        if p["job_id"] in self.jobs:
            issues.append(GateIssue("job_exists", "转码批次已存在", "payload.job_id"))
        return issues

    def _check_transcode_shard_completed(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        job = self.jobs.get(p["job_id"])
        if job is None:
            return [GateIssue("job_missing", "转码批次不存在", "payload.job_id")]
        idx = p["shard_index"]
        if not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < job["shard_count"]:
            return [GateIssue("shard_out_of_range", "分片序号越界", "payload.shard_index")]
        done = job["completed"].get(idx)
        if done is not None and done != p["output_hash"]:
            return [GateIssue("shard_hash_conflict", "同一分片已完成但输出哈希不同", "payload.output_hash")]
        return []

    def _check_dispute_resolved(self, e: Mapping[str, Any], p: Mapping[str, Any]) -> list[GateIssue]:
        key = next((k for k, entries in self.disputes.items() if entries[-1]["id"] == p["dispute_id"]), None)
        if key is None:
            return [GateIssue("dispute_missing", "争议不存在", "payload.dispute_id")]
        if self.disputes[key][-1]["status"] != "open":
            return [GateIssue("dispute_closed", "争议已结案", "payload.dispute_id")]
        if p["resolution"] not in ("keep_stored", "keep_received", "manual"):
            return [GateIssue("invalid_resolution", "结案方式非法", "payload.resolution")]
        return []

    # ---- 投影应用 ---------------------------------------------------------

    def _apply(self, event: Mapping[str, Any], cascade: bool) -> None:
        etype = event["event_type"]
        p = event["payload"]
        if etype == "COLLECTOR_QUALIFIED":
            self.qualifications[p["collector_id"]] = event
        elif etype == "COLLECTION_PLAN_APPROVED":
            self.plans[p["plan_id"]] = event
        elif etype == "PLACE_REGISTERED":
            self.places[event["aggregate_id"]] = {
                "id": event["aggregate_id"],
                "canonical_name": p["canonical_name"],
                "aliases": [],
                "active": True,
                "merged_into": None,
                "migrated_observations": [],
                "rejected_relations": [],
            }
        elif etype == "PLACE_ALIAS_RECORDED":
            self.places[event["aggregate_id"]]["aliases"].append(p["alias_name"])
        elif etype == "PLACES_MERGED":
            self._apply_merge(event)
        elif etype == "CONSENT_GRANTED":
            record = self.consents.setdefault(p["participant_id"], {"grants": [], "withdrawals": []})
            record["grants"].append(event)
        elif etype == "CONSENT_WITHDRAWN":
            self.consents[p["participant_id"]]["withdrawals"].append(event)
        elif etype == "RAW_FILE_RECEIVED":
            self.files[p["file_hash"]] = event
        elif etype == "OBSERVATION_UPLOADED":
            self.observations[event["aggregate_id"]] = {"event": event, "migrated_to": None}
        elif etype == "CLAIM_PROPOSED":
            self.claims[event["aggregate_id"]] = _ClaimState(event=event)
        elif etype == "CLAIM_REVIEWED":
            self.claims[p["claim_id"]].reviews.append(event)
        elif etype == "CORRECTION_FILED":
            target = p["target_ref"]
            if target in self.claims:
                self.claims[target].corrections.append(event)
            elif target in self.observations:
                self.observations[target].setdefault("corrections", []).append(event)
        elif etype == "ARCHIVE_RELEASED":
            self.claims[p["claim_id"]].releases.append(event)
        elif etype == "RESEARCH_USE_RECORDED":
            self.claims[p["claim_id"]].research_uses.append(event)
        elif etype == "ARCHIVE_ANNOTATED":
            self.claims[p["claim_id"]].annotations.append(event)
        elif etype == "RELEASE_RESTRICTED":
            self.claims[p["claim_id"]].restrictions.append(event)
        elif etype == "TRANSCODE_JOB_STARTED":
            self.jobs[p["job_id"]] = {"shard_count": p["shard_count"], "completed": {}}
        elif etype == "TRANSCODE_SHARD_COMPLETED":
            self.jobs[p["job_id"]]["completed"].setdefault(p["shard_index"], p["output_hash"])
        elif etype == "DISPUTE_OPENED":
            self.disputes.setdefault(p["idempotency_key"], []).append(
                {"id": event["aggregate_id"], "status": "open", "opened": event, "resolution": None}
            )
        elif etype == "DISPUTE_RESOLVED":
            for entries in self.disputes.values():
                if entries[-1]["id"] == p["dispute_id"]:
                    entries[-1]["status"] = "closed"
                    entries[-1]["resolution"] = event
                    break

    def _apply_merge(self, event: Mapping[str, Any]) -> None:
        p = event["payload"]
        survivor = self.places[p["surviving_place_id"]]
        at = parse_ts(event["occurred_at"])
        for absorbed_id in p["absorbed_place_ids"]:
            absorbed = self.places[absorbed_id]
            for obs_id, obs in self.observations.items():
                if obs["event"]["payload"]["place_id"] != absorbed_id or obs["migrated_to"]:
                    continue
                participant = obs["event"]["payload"]["participant_id"]
                if self.effective_scopes(participant, at):
                    survivor["migrated_observations"].append(obs_id)
                    obs["migrated_to"] = survivor["id"]
                else:
                    absorbed["rejected_relations"].append(
                        {"observation_id": obs_id, "reason": "consent_no_longer_effective"}
                    )
            absorbed["active"] = False
            absorbed["merged_into"] = survivor["id"]

    # ---- 派生级联 ---------------------------------------------------------

    def _cascades(self, event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        if event["event_type"] != "CONSENT_WITHDRAWN":
            return []
        p = event["payload"]
        at = parse_ts(p["effective_at"])
        derived: list[Mapping[str, Any]] = []
        affected_obs = {
            oid
            for oid, obs in self.observations.items()
            if obs["event"]["payload"]["participant_id"] == p["participant_id"]
        }
        for claim_id, claim in self.claims.items():
            chain = set(self.source_closure_ids(claim_id))
            if not (chain & affected_obs):
                continue
            scopes = set(p["affected_scope"])
            # 沿引用链登记限制：未发布档案不得发布这些范围，已发布范围不得续发
            derived.append(
                self._derived_event(
                    event, claim_id, "01", "RELEASE_RESTRICTED",
                    {
                        "claim_id": claim_id,
                        "restricted_scopes": sorted(scopes),
                        "reason_ref": event["event_id"],
                    },
                )
            )
            # 已发布或已用于研究的版本不能撤回删除，只能追加说明
            if claim.used_in_research or claim.releases:
                note = (
                    "受访者已撤回公开许可；本版本已用于研究，原始记录保留并追加此说明，"
                    "结论引用须同步标注授权变化。"
                    if claim.used_in_research
                    else "受访者已撤回公开许可；已发布页面加注，对应范围停止续发。"
                )
                derived.append(
                    self._derived_event(
                        event, claim_id, "02", "ARCHIVE_ANNOTATED",
                        {"claim_id": claim_id, "note": note, "reason_ref": event["event_id"]},
                    )
                )
        return derived

    @staticmethod
    def _derived_event(
        cause: Mapping[str, Any], claim_id: str, suffix: str, etype: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        return {
            "event_id": f"derived:{cause['event_id']}:{claim_id}:{suffix}",
            "event_type": etype,
            "aggregate_type": "archive_claim",
            "aggregate_id": claim_id,
            "occurred_at": cause["occurred_at"],
            "version": 1,
            "idempotency_key": f"system:{etype.lower()}:{cause['event_id']}:{claim_id}:{suffix}",
            "payload": payload,
        }

    # ---- 查询 -------------------------------------------------------------

    def effective_scopes(self, participant_id: str, at: datetime | None) -> set[str]:
        """某受访者在指定时刻仍有效的同意范围；at=None 表示当前（应用全部撤回）。"""
        record = self.consents.get(participant_id)
        if not record or not record["grants"]:
            return set()
        grant = record["grants"][0]
        for candidate in record["grants"]:
            cat = parse_ts(candidate["payload"]["granted_at"])
            if cat is not None and (at is None or cat <= at) and parse_ts(grant["payload"]["granted_at"]) <= cat:
                grant = candidate
        scopes = set(grant["payload"]["scope"])
        grant_at = parse_ts(grant["payload"]["granted_at"])
        for w in record["withdrawals"]:
            wat = parse_ts(w["payload"]["effective_at"])
            if wat is not None and grant_at <= wat and (at is None or wat <= at):
                scopes.difference_update(w["payload"]["affected_scope"])
        return scopes

    def source_closure(self, claim_id: str) -> list[dict[str, Any]]:
        """沿引用链展开解释到全部原始观察（解释可引用旧观察，不覆盖）。"""
        return [self.observations[oid] for oid in self.source_closure_ids(claim_id)]

    def source_closure_ids(self, claim_id: str) -> list[str]:
        seen_claims: set[str] = set()
        observations: dict[str, object] = {}
        stack = [claim_id]
        while stack:
            current = stack.pop()
            claim = self.claims.get(current)
            if claim is None or current in seen_claims:
                continue
            seen_claims.add(current)
            for ref in claim.event["payload"]["source_refs"]:
                if ref in self.observations:
                    observations[ref] = True
                elif ref in self.claims:
                    stack.append(ref)
        return list(observations)

    def pending_shards(self, job_id: str) -> list[int]:
        job = self.jobs[job_id]
        return [i for i in range(job["shard_count"]) if i not in job["completed"]]

    def open_disputes(self) -> list[dict[str, Any]]:
        return [entries[-1] for entries in self.disputes.values() if entries[-1]["status"] == "open"]

    def next_shard(self, job_id: str) -> int | None:
        pending = self.pending_shards(job_id)
        return pending[0] if pending else None
