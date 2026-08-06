from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError, ExitCode
from .state import StateStore
from .validation import assert_no_password_fields


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_report(store: StateStore, batch_id: str) -> dict[str, Any]:
    metadata = store.load_metadata(batch_id)
    entities = store.list_entities(batch_id)
    raw_calls = store.list_calls(batch_id)
    calls = [
        {
            "shardIndex": item["shard_index"],
            "startedAt": item["started_at"],
            "endedAt": item["ended_at"],
            "durationMs": item["duration_ms"],
            "outcome": item["outcome"],
            "errorCode": item["error_code"],
        }
        for item in raw_calls
    ]
    summary = dict(sorted(Counter(item["finalStatus"] for item in entities).items()))
    entity_summary: dict[str, dict[str, int]] = {}
    for item in entities:
        counts = entity_summary.setdefault(item["entityType"], {})
        counts[item["finalStatus"]] = counts.get(item["finalStatus"], 0) + 1
    success = bool(entities) and all(
        item["finalStatus"] in {"SUCCESS", "ALREADY_EXISTS"} for item in entities
    )
    report = {
        "generatedAt": _now(),
        "overallStatus": "SUCCESS" if success else "INCOMPLETE_OR_FAILED",
        "metadata": metadata,
        "sourceSnapshot": store.load_source_summary(batch_id),
        "summary": summary,
        "entitySummary": entity_summary,
        "entities": entities,
        "calls": calls,
        "runStartedAt": calls[0]["startedAt"] if calls else None,
        "runEndedAt": calls[-1]["endedAt"] if calls else None,
        "totalRuntimeMs": sum(item["durationMs"] for item in calls),
    }
    assert_no_password_fields(report)
    return report


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        rendered = ", ".join(str(item) for item in value)
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    metadata = report["metadata"]
    lines = [
        f"# Migration batch `{_cell(metadata['migrationBatchId'])}`",
        "",
        f"- Overall status: `{_cell(report['overallStatus'])}`",
        f"- Legacy tenant ID: `{_cell(metadata['legacyTenantId'])}`",
        f"- Enabled modules: `{_cell(metadata['enabledModules'])}`",
        f"- Source timezone: `{_cell(metadata['sourceTimezone'])}`",
        f"- Snapshot at: `{_cell(metadata['snapshotAt'])}`",
        f"- Run started at: `{_cell(report['runStartedAt'])}`",
        f"- Run ended at: `{_cell(report['runEndedAt'])}`",
        f"- Total HTTP runtime: `{_cell(report['totalRuntimeMs'])} ms`",
        "",
        "## Result summary",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    for status, count in report["summary"].items():
        lines.append(f"| {_cell(status)} | {_cell(count)} |")
    lines.extend(
        [
            "",
            "## Entity ledger",
            "",
            "| Entity type | Source ID | Correlation ID | Target ID | Attempts | Final status | Error code | Audit codes | Shard | Runtime (ms) |",
            "|---|---|---|---|---:|---|---|---|---:|---:|",
        ]
    )
    for item in report["entities"]:
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    item["entityType"],
                    item["sourceId"],
                    item["correlationId"],
                    item["targetId"],
                    item["attemptCount"],
                    item["finalStatus"],
                    item["errorCode"],
                    item["auditCodes"],
                    item["lastShardIndex"],
                    item["lastDurationMs"],
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Shard calls",
            "",
            "| Shard | Started at | Ended at | Runtime (ms) | Outcome | Error code |",
            "|---:|---|---|---:|---|---|",
        ]
    )
    for call in report["calls"]:
        lines.append(
            "| "
            + " | ".join(
                _cell(value)
                for value in (
                    call["shardIndex"],
                    call["startedAt"],
                    call["endedAt"],
                    call["durationMs"],
                    call["outcome"],
                    call["errorCode"],
                )
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def write_reports(
    store: StateStore, batch_id: str, output_dir: str | Path
) -> tuple[Path, Path]:
    report = build_report(store, batch_id)
    directory = Path(output_dir).resolve()
    old_umask = os.umask(0o077)
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        suffix = hashlib.sha256(batch_id.encode("utf-8")).hexdigest()[:12]
        json_path = directory / f"migration-report-{suffix}.json"
        markdown_path = directory / f"migration-report-{suffix}.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
    except OSError as error:
        raise ConfigError("REPORT_WRITE_FAILED") from error
    finally:
        os.umask(old_umask)
    json_path.chmod(0o600)
    markdown_path.chmod(0o600)
    return json_path, markdown_path


def report_exit_code(report: dict[str, Any]) -> ExitCode:
    if report["overallStatus"] == "SUCCESS":
        return ExitCode.SUCCESS
    if report["calls"] and report["calls"][-1]["outcome"] in {
        "TRANSPORT_ERROR",
        "PROTOCOL_ERROR",
    }:
        return ExitCode.NETWORK_PROTOCOL
    return ExitCode.PARTIAL_FAILURE
