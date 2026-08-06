from __future__ import annotations

import copy
import hashlib
from typing import Any

from .errors import ConfigError
from .validation import assert_no_password_fields, topological_sort, validate_snapshot


def stable_correlation_id(batch_id: str, entity_type: str, source_id: str) -> str:
    material = f"{batch_id}\0{entity_type}\0{source_id}".encode("utf-8")
    digest = hashlib.sha256(material).hexdigest()[:40]
    return f"etbc-{entity_type.lower()}-{digest}"


def _item(batch_id: str, entity_type: str, source_id: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "correlationId": stable_correlation_id(batch_id, entity_type, source_id),
        "sourceId": source_id,
        "data": copy.deepcopy(data),
    }


def build_shards(
    snapshot: dict[str, Any], metadata: dict[str, Any], staff_chunk_size: int = 150
) -> list[dict[str, Any]]:
    if staff_chunk_size < 1:
        raise ConfigError("STAFF_CHUNK_SIZE_INVALID")
    validate_snapshot(snapshot, metadata)
    batch_id = metadata["migrationBatchId"]
    tenant = snapshot["tenant"]
    tenant_item = _item(batch_id, "TENANT", str(tenant["participantId"]), tenant)
    organizations = [
        _item(batch_id, "ORGANIZATION", str(organization["id"]), organization)
        for organization in topological_sort(snapshot["organizations"])
    ]
    staff = [
        _item(batch_id, "STAFF", str(person["id"]), person)
        for person in sorted(snapshot["staff"], key=lambda item: int(item["id"]))
    ]
    chunks = [staff[index : index + staff_chunk_size] for index in range(0, len(staff), staff_chunk_size)]
    if not chunks:
        chunks = [[]]
    base = {
        "migrationBatchId": metadata["migrationBatchId"],
        "legacyTenantId": metadata["legacyTenantId"],
        "enabledModules": list(metadata["enabledModules"]),
        "sourceTimezone": metadata["sourceTimezone"],
        "snapshotAt": metadata["snapshotAt"],
    }
    shards = [
        {
            **base,
            "tenants": [copy.deepcopy(tenant_item)],
            "organizations": copy.deepcopy(organizations),
            "staff": copy.deepcopy(chunk),
        }
        for chunk in chunks
    ]
    assert_no_password_fields(shards)
    return shards
