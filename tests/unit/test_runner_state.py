from __future__ import annotations

from pathlib import Path

import pytest

from etbc_migration.errors import ConfigError, NetworkProtocolError, PartialFailureError, TransportError
from etbc_migration.payloads import build_shards
from etbc_migration.runner import MigrationRunner
from etbc_migration.state import StateStore

from .test_iam_client import success_response


class ScriptedClient:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def import_batch(self, payload: dict) -> list[dict]:
        self.payloads.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def item_results(payload: dict, overrides: dict[tuple[str, str], str] | None = None) -> list[dict]:
    response = success_response(payload)["data"]["items"]
    overrides = overrides or {}
    for item in response:
        status = overrides.get((item["entityType"], item["sourceId"]))
        if status:
            item["status"] = status
            item["errorCode"] = f"{status}_CODE"
            if status not in {"SUCCESS", "ALREADY_EXISTS"}:
                item["targetId"] = None
    return response


def create_store(tmp_path: Path, source_snapshot: dict, metadata: dict, chunk_size: int = 150) -> StateStore:
    store = StateStore(tmp_path)
    store.create_snapshot(
        metadata,
        build_shards(source_snapshot, metadata, chunk_size),
        {"capturedAt": source_snapshot["captured_at"], "staffCount": len(source_snapshot["staff"])},
    )
    return store


def test_process_failed_retries_but_validation_failed_does_not(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    payload = store.load_shards(metadata["migrationBatchId"])[0]
    first = item_results(
        payload,
        {("STAFF", "100"): "PROCESS_FAILED", ("STAFF", "101"): "VALIDATION_FAILED"},
    )
    retry_payload = dict(payload)
    retry_payload["staff"] = [item for item in payload["staff"] if item["sourceId"] == "100"]
    second = item_results(retry_payload)
    client = ScriptedClient([first, second])

    with pytest.raises(PartialFailureError):
        MigrationRunner(store, client, sleeper=lambda _: None).run(
            metadata["migrationBatchId"], max_attempts=3
        )

    assert len(client.payloads) == 2
    assert [item["sourceId"] for item in client.payloads[1]["staff"]] == ["100"]
    entities = {(item["entityType"], item["sourceId"]): item for item in store.list_entities("batch-001")}
    assert entities[("STAFF", "100")]["attemptCount"] == 2
    assert entities[("STAFF", "100")]["finalStatus"] == "SUCCESS"
    assert entities[("STAFF", "101")]["attemptCount"] == 1
    assert entities[("STAFF", "101")]["finalStatus"] == "VALIDATION_FAILED"


def test_resume_skips_success_and_retries_process_failure(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    payload = store.load_shards("batch-001")[0]
    first_client = ScriptedClient([item_results(payload, {("STAFF", "102"): "PROCESS_FAILED"})])
    with pytest.raises(PartialFailureError):
        MigrationRunner(store, first_client, sleeper=lambda _: None).run("batch-001", max_attempts=1)

    retry_payload = dict(payload)
    retry_payload["staff"] = [item for item in payload["staff"] if item["sourceId"] == "102"]
    second_client = ScriptedClient([item_results(retry_payload)])
    MigrationRunner(store, second_client, sleeper=lambda _: None).run("batch-001", max_attempts=2)

    assert [item["sourceId"] for item in second_client.payloads[0]["staff"]] == ["102"]
    entities = {(item["entityType"], item["sourceId"]): item for item in store.list_entities("batch-001")}
    assert entities[("STAFF", "100")]["attemptCount"] == 1
    assert entities[("STAFF", "102")]["attemptCount"] == 2


def test_state_permissions_are_restricted(tmp_path: Path) -> None:
    state_dir = tmp_path / "private-state"
    store = StateStore(state_dir)
    try:
        assert state_dir.stat().st_mode & 0o777 == 0o700
        assert store.path.stat().st_mode & 0o777 == 0o600
    finally:
        store.close()


def test_state_rejects_symlink_directory(tmp_path: Path) -> None:
    real_directory = tmp_path / "real-state"
    real_directory.mkdir()
    linked_directory = tmp_path / "linked-state"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ConfigError, match="STATE_DIRECTORY_SYMLINK_FORBIDDEN"):
        StateStore(linked_directory)


def test_state_rejects_symlink_database(tmp_path: Path) -> None:
    state_directory = tmp_path / "state"
    state_directory.mkdir()
    unrelated_file = tmp_path / "unrelated.sqlite3"
    unrelated_file.touch()
    (state_directory / "migration-state.sqlite3").symlink_to(unrelated_file)

    with pytest.raises(ConfigError, match="STATE_DATABASE_SYMLINK_FORBIDDEN"):
        StateStore(state_directory)


def test_transport_timeout_retries_identical_payload_with_exponential_backoff(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    payload = store.load_shards("batch-001")[0]
    delays: list[float] = []
    client = ScriptedClient(
        [TransportError("IAM_TRANSPORT_ERROR"), TransportError("IAM_TRANSPORT_ERROR"), item_results(payload)]
    )

    MigrationRunner(store, client, sleeper=delays.append, base_delay_seconds=0.25).run(
        "batch-001", max_attempts=3
    )

    assert client.payloads == [payload, payload, payload]
    assert delays == [0.25, 0.5]
    assert store.list_entities("batch-001")[0]["attemptCount"] == 3


def test_protocol_error_is_not_retried(tmp_path: Path, source_snapshot: dict, metadata: dict) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    client = ScriptedClient([NetworkProtocolError("IAM_RESULT_ITEM_SET_MISMATCH")])
    with pytest.raises(NetworkProtocolError):
        MigrationRunner(store, client, sleeper=lambda _: None).run("batch-001", max_attempts=3)
    assert len(client.payloads) == 1


def test_existing_batch_metadata_is_immutable(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    changed = dict(metadata)
    changed["sourceTimezone"] = "UTC"
    with pytest.raises(Exception, match="MIGRATION_BATCH_METADATA_MISMATCH"):
        store.create_snapshot(
            changed,
            store.load_shards(metadata["migrationBatchId"]),
            {"capturedAt": source_snapshot["captured_at"]},
        )


def test_existing_batch_source_summary_is_immutable(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    with pytest.raises(Exception, match="MIGRATION_SNAPSHOT_MISMATCH"):
        store.create_snapshot(
            metadata,
            store.load_shards(metadata["migrationBatchId"]),
            {"capturedAt": source_snapshot["captured_at"], "staffCount": 999},
        )


def test_state_lists_batches_without_exposing_payloads(
    tmp_path: Path, source_snapshot: dict, metadata: dict
) -> None:
    store = create_store(tmp_path, source_snapshot, metadata)
    try:
        batches = store.list_batches()
    finally:
        store.close()

    assert batches == [
        {
            "batchId": "batch-001",
            "legacyTenantId": "tenant-001",
            "snapshotAt": "2026-07-30T00:40:00Z",
            "createdAt": batches[0]["createdAt"],
            "updatedAt": batches[0]["updatedAt"],
        }
    ]
    assert "payload" not in repr(batches).lower()
