from __future__ import annotations

from etbc_migration.payloads import build_shards, stable_correlation_id


def test_correlation_id_is_stable_and_entity_scoped() -> None:
    first = stable_correlation_id("batch-001", "STAFF", "00042")
    assert first == stable_correlation_id("batch-001", "STAFF", "00042")
    assert first != stable_correlation_id("batch-001", "ORGANIZATION", "00042")
    assert len(first) <= 64


def test_staff_shards_repeat_tenant_and_complete_organization_closure(
    source_snapshot: dict, metadata: dict
) -> None:
    shards = build_shards(source_snapshot, metadata, staff_chunk_size=2)
    assert len(shards) == 2
    assert [len(shard["staff"]) for shard in shards] == [2, 1]
    assert all(len(shard["tenants"]) == 1 for shard in shards)
    assert all([item["sourceId"] for item in shard["organizations"]] == ["10", "11"] for shard in shards)
    assert shards[0]["organizations"] == shards[1]["organizations"]
    assert all(shard["migrationBatchId"] == metadata["migrationBatchId"] for shard in shards)


def test_no_staff_still_produces_tenant_organization_shard(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["staff"] = []
    shards = build_shards(source_snapshot, metadata, staff_chunk_size=150)
    assert len(shards) == 1
    assert shards[0]["staff"] == []


def test_payload_never_contains_password_fields(source_snapshot: dict, metadata: dict) -> None:
    rendered = repr(build_shards(source_snapshot, metadata, staff_chunk_size=150)).lower()
    assert "loginpwd" not in rendered
    assert "password" not in rendered
