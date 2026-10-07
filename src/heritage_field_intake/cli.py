"""命令行入口。

子命令：
  validate <schema.json> <event.json>     校验单个事件（兼容旧用法）
  ingest <ledger.json> <batch.json...>    按稳定事件键补传入库，回写账本
  audit <ledger.json> <claim_id>          从公开叙述回溯全链路
  resume <ledger.json> <job_id>           输出转码批次下一个未完成分片
  disputes <ledger.json>                  列出未决争议
不带子命令时按旧版 `cli <schema> <event>` 方式运行。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .audit import build_audit
from .contracts import validate_event
from .gate import Gate, GateError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA = ROOT / "contracts" / "domain.schema.json"


def _load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_schema(path: str | Path | None) -> dict[str, Any]:
    return _load_json(path) if path else _load_json(DEFAULT_SCHEMA)


def _cmd_validate(args: list[str]) -> int:
    if len(args) != 2:
        print("用法: validate <schema.json> <event.json>", file=sys.stderr)
        return 2
    schema = _load_json(args[0])
    event = _load_json(args[1])
    issues = validate_event(event, schema)
    if not issues:
        print("valid")
        return 0
    for issue in issues:
        print(f"{issue.field}\t{issue.code}\t{issue.message}")
    return 1


def _load_ledger(path: Path, schema: dict[str, Any]) -> Gate:
    if not path.exists():
        return Gate(schema)
    return Gate.from_file(path, schema)


def _cmd_ingest(args: list[str]) -> int:
    if len(args) < 2:
        print("用法: ingest <ledger.json> <batch.json...> [--schema <schema.json>]", file=sys.stderr)
        return 2
    schema_path: str | None = None
    if "--schema" in args:
        i = args.index("--schema")
        schema_path = args[i + 1]
        args = args[:i] + args[i + 2 :]

    ledger_path = Path(args[0])
    schema = _load_schema(schema_path)
    try:
        gate = _load_ledger(ledger_path, schema)
    except GateError as exc:
        print(f"ledger_error\t{exc}", file=sys.stderr)
        return 2

    summary = {"accepted": 0, "duplicate": 0, "disputed": 0, "rejected": 0, "derived": 0}
    batch_path: str | None = None
    try:
        for batch_path in args[1:]:
            batch = _load_json(batch_path)
            events = batch if isinstance(batch, list) else [batch]
            for event in events:
                result = gate.ingest(event)
                summary[result.status] += 1
                summary["derived"] += len(result.derived)
                line = {
                    "status": result.status,
                    "event_id": result.event_id,
                    "batch": batch_path,
                }
                if result.dispute_id:
                    line["dispute_id"] = result.dispute_id
                if result.issues:
                    line["issues"] = [
                        {"field": i.field, "code": i.code, "message": i.message} for i in result.issues
                    ]
                print(json.dumps(line, ensure_ascii=False))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"batch_error\t{batch_path}\t{exc}", file=sys.stderr)
        return 2

    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(gate.events, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"summary": summary, "ledger": str(ledger_path)}, ensure_ascii=False))
    return 0 if summary["rejected"] == 0 else 1


def _cmd_audit(args: list[str]) -> int:
    schema_path: str | None = None
    if "--schema" in args:
        i = args.index("--schema")
        if i + 1 >= len(args):
            print("用法: audit <ledger.json> <claim_id> [--schema <schema.json>]", file=sys.stderr)
            return 2
        schema_path = args[i + 1]
        args = args[:i] + args[i + 2 :]
    if len(args) != 2:
        print("用法: audit <ledger.json> <claim_id> [--schema <schema.json>]", file=sys.stderr)
        return 2
    ledger_path, claim_id = Path(args[0]), args[1]
    gate = Gate.from_file(ledger_path, _load_schema(schema_path))
    try:
        report = build_audit(gate, claim_id)
    except KeyError as exc:
        print(f"not_found\t{exc.args[0]}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def _cmd_resume(args: list[str]) -> int:
    if len(args) != 2:
        print("用法: resume <ledger.json> <job_id>", file=sys.stderr)
        return 2
    gate = Gate.from_file(Path(args[0]), _load_schema(None))
    if args[1] not in gate.jobs:
        print(f"job_missing\t{args[1]}", file=sys.stderr)
        return 1
    pending = gate.pending_shards(args[1])
    print(json.dumps({"job_id": args[1], "next_shard": pending[0] if pending else None,
                      "pending": pending}, ensure_ascii=False))
    return 0


def _cmd_disputes(args: list[str]) -> int:
    if len(args) != 1:
        print("用法: disputes <ledger.json>", file=sys.stderr)
        return 2
    gate = Gate.from_file(Path(args[0]), _load_schema(None))
    rows = [
        {
            "dispute_id": item["id"],
            "status": item["status"],
            "stored_event_id": item["opened"]["payload"]["stored_event_id"],
            "received_event_id": item["opened"]["payload"]["received_event_id"],
            "reason": item["opened"]["payload"]["reason"],
        }
        for item in gate.open_disputes()
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0 if rows else 1


COMMANDS = {
    "validate": _cmd_validate,
    "ingest": _cmd_ingest,
    "audit": _cmd_audit,
    "resume": _cmd_resume,
    "disputes": _cmd_disputes,
}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        return COMMANDS[args[0]](args[1:])
    # 兼容旧用法：cli <schema.json> <event.json>
    if len(args) == 2:
        return _cmd_validate(args)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
