from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from etbc_migration.errors import LocalValidationError
from etbc_migration.validation import topological_sort, validate_modules, validate_snapshot


def test_topological_sort_places_parent_before_child(source_snapshot: dict) -> None:
    organizations = list(reversed(source_snapshot["organizations"]))
    assert [row["id"] for row in topological_sort(organizations)] == [10, 11]


def test_topological_sort_rejects_missing_parent(source_snapshot: dict) -> None:
    organizations = copy.deepcopy(source_snapshot["organizations"])
    organizations[1]["parentOrgId"] = 999
    with pytest.raises(LocalValidationError, match="PARENT_ORG_SOURCE_MISSING"):
        topological_sort(organizations)


def test_topological_sort_rejects_cycle(source_snapshot: dict) -> None:
    organizations = copy.deepcopy(source_snapshot["organizations"])
    organizations[0]["parentOrgId"] = 11
    with pytest.raises(LocalValidationError, match="ORG_TOPOLOGY_CYCLE"):
        topological_sort(organizations)


def test_modules_must_be_exact_supported_core_set() -> None:
    assert validate_modules(["TENANT", "ORGANIZATION", "STAFF"]) == (
        "TENANT",
        "ORGANIZATION",
        "STAFF",
    )
    with pytest.raises(LocalValidationError, match="MODULE_DEPENDENCY_MISSING_STAFF"):
        validate_modules(["STAFF"])
    with pytest.raises(LocalValidationError, match="MODULE_NOT_IMPLEMENTED_TENANT_ROLE"):
        validate_modules(["TENANT", "ORGANIZATION", "STAFF", "TENANT_ROLE"])
    with pytest.raises(LocalValidationError, match="UNKNOWN_MODULE"):
        validate_modules(["TENANT", "ORGANIZATION", "STAFF", "TYPO"])


def test_snapshot_rejects_staff_outside_organization_closure(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["staff"][0]["orgnizationId"] = 999
    with pytest.raises(LocalValidationError, match="STAFF_ORG_SOURCE_MISSING"):
        validate_snapshot(source_snapshot, metadata)


def test_snapshot_rejects_duplicate_source_id(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["staff"][1]["id"] = source_snapshot["staff"][0]["id"]
    with pytest.raises(LocalValidationError, match="STAFF_SOURCE_ID_DUPLICATE"):
        validate_snapshot(source_snapshot, metadata)


def test_snapshot_rejects_password_fields_at_any_depth(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["staff"][0]["loginPwdEncrypt"] = "must-not-survive"
    with pytest.raises(LocalValidationError, match="PASSWORD_FIELD_FORBIDDEN"):
        validate_snapshot(source_snapshot, metadata)


def test_empty_email_and_gender_are_valid(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["staff"][0]["email"] = None
    source_snapshot["staff"][0]["genderId"] = None
    validate_snapshot(source_snapshot, metadata)


def test_snapshot_allows_small_database_clock_skew(source_snapshot: dict, metadata: dict) -> None:
    metadata["snapshotAt"] = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    validate_snapshot(source_snapshot, metadata)


def test_snapshot_rejects_source_time_after_snapshot(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["tenant"]["lastUpdateDate"] = "2027-01-01T00:00:00"
    with pytest.raises(LocalValidationError, match="SOURCE_TIME_AFTER_SNAPSHOT"):
        validate_snapshot(source_snapshot, metadata)


def test_snapshot_rejects_reversed_source_times(source_snapshot: dict, metadata: dict) -> None:
    source_snapshot["organizations"][0]["createDate"] = "2026-07-30T09:00:00"
    source_snapshot["organizations"][0]["lastUpdateDate"] = "2026-07-30T08:00:00"
    with pytest.raises(LocalValidationError, match="SOURCE_TIME_ORDER_INVALID"):
        validate_snapshot(source_snapshot, metadata)


def test_snapshot_rejects_ambiguous_source_local_time(source_snapshot: dict, metadata: dict) -> None:
    metadata["sourceTimezone"] = "America/New_York"
    metadata["snapshotAt"] = "2026-07-30T23:00:00Z"
    source_snapshot["staff"][0]["createDate"] = "2025-11-02T01:30:00"
    with pytest.raises(LocalValidationError, match="SOURCE_TIMEZONE_LOCAL_TIME_AMBIGUOUS"):
        validate_snapshot(source_snapshot, metadata)
