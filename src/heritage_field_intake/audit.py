"""命令行审计：从一条公开叙述回溯全部证据链。

给定一个已发布的解释（archive_claim），返回其原始采集、受访者授权、
专业复核、纠错、公开范围变迁，以及针对同一批原始材料被否决的全部解释。
"""

from __future__ import annotations

from typing import Any, Mapping

from .gate import Gate, REVIEW_REJECTED, SCOPE_PUBLIC


def _event_brief(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": event["event_id"],
        "event_type": event["event_type"],
        "aggregate_id": event["aggregate_id"],
        "occurred_at": event["occurred_at"],
        "idempotency_key": event.get("idempotency_key"),
        "payload": event["payload"],
    }


def _place_lineage(gate: Gate, place_id: str) -> list[dict[str, Any]]:
    chain: list[dict[str, Any]] = []
    current: str | None = place_id
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        place = gate.places.get(current)
        if place is None:
            break
        chain.append(
            {
                "place_id": current,
                "canonical_name": place["canonical_name"],
                "aliases": list(place["aliases"]),
                "active": place["active"],
                "merged_into": place["merged_into"],
                "migrated_observations": list(place["migrated_observations"]),
                "rejected_relations": list(place["rejected_relations"]),
            }
        )
        current = place["merged_into"]
    return chain


def build_audit(gate: Gate, claim_id: str) -> dict[str, Any]:
    claim = gate.claims.get(claim_id)
    if claim is None:
        raise KeyError(f"解释不存在: {claim_id}")

    observations = gate.source_closure(claim_id)
    observation_ids = {o["event"]["aggregate_id"] for o in observations}
    participants = {o["event"]["payload"]["participant_id"] for o in observations}
    file_hashes = {o["event"]["payload"]["file_hash"] for o in observations}
    places = {o["event"]["payload"]["place_id"] for o in observations}

    # 针对同一批原始材料的全部被否决解释（含本解释自身历史上的否决）
    rejected: list[dict[str, Any]] = []
    for other_id, other in gate.claims.items():
        rejected_reviews = [r for r in other.reviews if r["payload"]["decision"] == REVIEW_REJECTED]
        if not rejected_reviews:
            continue
        shared = set(gate.source_closure_ids(other_id)) & observation_ids
        if other_id == claim_id or shared:
            rejected.append(
                {
                    "claim_id": other_id,
                    "statement": other.event["payload"]["statement"],
                    "proposer_id": other.event["payload"]["proposer_id"],
                    "proposed_event": _event_brief(other.event),
                    "source_refs": other.event["payload"]["source_refs"],
                    "shared_observations": sorted(shared),
                    "reviews": [_event_brief(r) for r in rejected_reviews],
                    "corrections": [_event_brief(c) for c in other.corrections],
                }
            )

    sources = []
    involved_keys: set[str] = set()
    for obs_state in observations:
        obs = obs_state["event"]
        if key := obs.get("idempotency_key"):
            involved_keys.add(key)
        file_event = gate.files.get(obs["payload"]["file_hash"])
        if file_event and file_event.get("idempotency_key"):
            involved_keys.add(file_event["idempotency_key"])
        sources.append(
            {
                "observation": _event_brief(obs),
                "raw_file": _event_brief(file_event) if file_event else None,
                "place_lineage": _place_lineage(gate, obs["payload"]["place_id"]),
                "migrated_to": obs_state.get("migrated_to"),
                "corrections": [_event_brief(c) for c in obs_state.get("corrections", [])],
            }
        )

    consents = {}
    for participant in sorted(participants):
        record = gate.consents.get(participant, {"grants": [], "withdrawals": []})
        consents[participant] = {
            "grants": [_event_brief(g) for g in record["grants"]],
            "withdrawals": [_event_brief(w) for w in record["withdrawals"]],
            "current_scopes": sorted(gate.effective_scopes(participant, None)),
        }
        for grp in (record["grants"], record["withdrawals"]):
            for ev in grp:
                if key := ev.get("idempotency_key"):
                    involved_keys.add(key)

    return {
        "public_narrative": {
            "claim_id": claim_id,
            "statement": claim.event["payload"]["statement"],
            "proposer_id": claim.event["payload"]["proposer_id"],
            "proposed_event": _event_brief(claim.event),
            "status": claim.status,
            "released_scopes": sorted(claim.released_scopes),
            "is_public": SCOPE_PUBLIC in claim.released_scopes,
        },
        "reviews": [_event_brief(r) for r in claim.reviews],
        "sources": sources,
        "consents": consents,
        "place_history": {place_id: _place_lineage(gate, place_id) for place_id in sorted(places)},
        "raw_files": [
            _event_brief(gate.files[h]) for h in sorted(file_hashes) if h in gate.files
        ],
        "corrections": [_event_brief(c) for c in claim.corrections],
        "releases": [_event_brief(r) for r in claim.releases],
        "research_uses": [_event_brief(u) for u in claim.research_uses],
        "annotations": [_event_brief(a) for a in claim.annotations],
        "restrictions": [_event_brief(r) for r in claim.restrictions],
        "rejected_claims": sorted(rejected, key=lambda item: item["claim_id"]),
        "disputes": [
            {
                **_event_brief(entries[-1]["opened"]),
                "status": entries[-1]["status"],
                "resolution": _event_brief(entries[-1]["resolution"])
                if entries[-1]["resolution"]
                else None,
            }
            for key, entries in sorted(gate.disputes.items())
            if key in involved_keys
        ],
    }
