from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from etbc_migration.payloads import build_shards
from etbc_migration.reporting import build_report, write_reports
from etbc_migration.state import StateStore

from .test_runner_state import item_results


def test_report_contains_required_ledger_and_runtime_fields(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = StateStore(tmp_path / "state")
    payload = build_shards(source_snapshot, metadata, 150)[0]
    store.create_snapshot(
        metadata,
        [payload],
        {
            "capturedAt": source_snapshot["captured_at"],
            "tenantCount": 1,
            "organizationCount": 2,
            "staffCount": 3,
        },
    )
    started = store.mark_attempt("batch-001", 0, payload)
    store.record_results("batch-001", 0, 17, item_results(payload))
    store.record_call("batch-001", 0, started, 17, "RESPONSE")

    report = build_report(store, "batch-001")

    assert report["metadata"] == metadata
    assert report["summary"]["SUCCESS"] == 6
    assert report["entities"][0].keys() >= {
        "entityType",
        "sourceId",
        "correlationId",
        "targetId",
        "attemptCount",
        "finalStatus",
        "errorCode",
        "auditCodes",
        "firstShardIndex",
        "lastShardIndex",
        "lastDurationMs",
    }
    assert report["calls"][0]["durationMs"] == 17


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are unavailable on Windows")
def test_machine_and_human_reports_are_private(tmp_path: Path, source_snapshot: dict, metadata: dict) -> None:
    store = StateStore(tmp_path / "state")
    payload = build_shards(source_snapshot, metadata, 150)[0]
    store.create_snapshot(metadata, [payload], {"capturedAt": source_snapshot["captured_at"]})

    json_path, markdown_path = write_reports(store, "batch-001", tmp_path / "reports")

    assert json_path.stat().st_mode & 0o777 == 0o600
    assert markdown_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(json_path.read_text(encoding="utf-8"))["metadata"]["migrationBatchId"] == "batch-001"
    assert "Migration batch `batch-001`" in markdown_path.read_text(encoding="utf-8")
