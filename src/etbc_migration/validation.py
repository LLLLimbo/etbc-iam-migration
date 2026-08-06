from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import LocalValidationError


CORE_MODULES = ("TENANT", "ORGANIZATION", "STAFF")
MODULE_DEPENDENCIES: dict[str, set[str]] = {
    "TENANT": set(),
    "ORGANIZATION": {"TENANT"},
    "STAFF": {"ORGANIZATION"},
    "PRODUCT_RESOURCE": {"TENANT"},
    "PERMISSION_FEATURE": {"PRODUCT_RESOURCE"},
    "TENANT_ROLE": {"ORGANIZATION"},
    "ROLE_RESOURCE": {"TENANT_ROLE", "PERMISSION_FEATURE"},
    "USER_ROLE": {"STAFF", "TENANT_ROLE"},
    "PRESET_ROLE_BINDING": {"STAFF"},
    "POSITION": {"ORGANIZATION"},
    "STAFF_POSITION": {"POSITION", "STAFF"},
}
IMPLEMENTED_MODULES = set(CORE_MODULES)
MAX_SNAPSHOT_CLOCK_SKEW = timedelta(seconds=5)


def validate_modules(raw_modules: list[str] | tuple[str, ...] | set[str]) -> tuple[str, ...]:
    if not raw_modules:
        raise LocalValidationError("ENABLED_MODULES_REQUIRED")
    modules = {str(module).strip() for module in raw_modules if str(module).strip()}
    if len(modules) != len(raw_modules):
        raise LocalValidationError("ENABLED_MODULES_DUPLICATE_OR_EMPTY")
    unknown = sorted(modules - MODULE_DEPENDENCIES.keys())
    if unknown:
        raise LocalValidationError("UNKNOWN_MODULE")
    for module in sorted(modules):
        if not MODULE_DEPENDENCIES[module].issubset(modules):
            raise LocalValidationError(f"MODULE_DEPENDENCY_MISSING_{module}")
    unimplemented = sorted(modules - IMPLEMENTED_MODULES)
    if unimplemented:
        raise LocalValidationError(f"MODULE_NOT_IMPLEMENTED_{unimplemented[0]}")
    if modules != IMPLEMENTED_MODULES:
        raise LocalValidationError("ENABLED_MODULES_MUST_EQUAL_CORE_SET")
    return CORE_MODULES


def assert_no_password_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if "password" in normalized or "loginpwd" in normalized:
                raise LocalValidationError("PASSWORD_FIELD_FORBIDDEN")
            assert_no_password_fields(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            assert_no_password_fields(child)


def _source_id(value: Any, code: str) -> str:
    if value is None or isinstance(value, bool):
        raise LocalValidationError(code)
    rendered = str(value)
    if not rendered:
        raise LocalValidationError(code)
    return rendered


def topological_sort(organizations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for organization in organizations:
        source_id = _source_id(organization.get("id"), "ORG_SOURCE_ID_REQUIRED")
        if source_id in indexed:
            raise LocalValidationError("ORG_SOURCE_ID_DUPLICATE")
        indexed[source_id] = organization
    visiting: set[str] = set()
    visited: set[str] = set()
    ordered: list[dict[str, Any]] = []

    def visit(source_id: str) -> None:
        if source_id in visited:
            return
        if source_id in visiting:
            raise LocalValidationError("ORG_TOPOLOGY_CYCLE")
        visiting.add(source_id)
        parent = indexed[source_id].get("parentOrgId")
        if parent not in (None, 0):
            parent_id = str(parent)
            if parent_id not in indexed:
                raise LocalValidationError("PARENT_ORG_SOURCE_MISSING")
            visit(parent_id)
        visiting.remove(source_id)
        visited.add(source_id)
        ordered.append(indexed[source_id])

    for current_id in indexed:
        visit(current_id)
    return ordered


def _required_text(value: Any, code: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LocalValidationError(code)
    normalized = value.strip()
    if len(normalized) > maximum:
        raise LocalValidationError(code.replace("_REQUIRED", "_TOO_LONG"))
    return normalized


def _bounded(value: Any, maximum: int, code: str) -> None:
    if value is not None and (not isinstance(value, str) or len(value.strip()) > maximum):
        raise LocalValidationError(code)


def _local_time(value: Any, code: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LocalValidationError(code)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise LocalValidationError(code) from error
    if parsed.tzinfo is not None:
        raise LocalValidationError(code)
    return parsed


def _validate_metadata(metadata: dict[str, Any]) -> tuple[ZoneInfo, datetime]:
    _required_text(metadata.get("migrationBatchId"), "MIGRATION_BATCH_ID_REQUIRED", 64)
    _required_text(metadata.get("legacyTenantId"), "LEGACY_TENANT_ID_REQUIRED", 36)
    validate_modules(metadata.get("enabledModules") or [])
    timezone_name = _required_text(metadata.get("sourceTimezone"), "SOURCE_TIMEZONE_REQUIRED", 64)
    try:
        source_zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise LocalValidationError("SOURCE_TIMEZONE_INVALID") from error
    snapshot_at = metadata.get("snapshotAt")
    if not isinstance(snapshot_at, str):
        raise LocalValidationError("SNAPSHOT_AT_INVALID")
    try:
        parsed = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise LocalValidationError("SNAPSHOT_AT_INVALID") from error
    if parsed.tzinfo is None:
        raise LocalValidationError("SNAPSHOT_AT_INVALID")
    snapshot_utc = parsed.astimezone(timezone.utc)
    if snapshot_utc > datetime.now(timezone.utc) + MAX_SNAPSHOT_CLOCK_SKEW:
        raise LocalValidationError("SNAPSHOT_AT_INVALID")
    snapshot_utc = snapshot_utc.replace(microsecond=(snapshot_utc.microsecond // 1000) * 1000)
    return source_zone, snapshot_utc


def _source_instant(value: Any, source_zone: ZoneInfo, code: str) -> datetime | None:
    parsed = _local_time(value, code)
    if parsed is None:
        return None
    first = parsed.replace(tzinfo=source_zone, fold=0)
    second = parsed.replace(tzinfo=source_zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise LocalValidationError("SOURCE_TIMEZONE_LOCAL_TIME_AMBIGUOUS")
    return first.astimezone(timezone.utc)


def _validate_source_times(
    item: dict[str, Any], source_zone: ZoneInfo, snapshot_at: datetime, code: str
) -> None:
    created_at = _source_instant(item.get("createDate"), source_zone, code)
    updated_at = _source_instant(item.get("lastUpdateDate"), source_zone, code)
    if created_at is not None and updated_at is not None and updated_at < created_at:
        raise LocalValidationError("SOURCE_TIME_ORDER_INVALID")
    if any(value is not None and value > snapshot_at for value in (created_at, updated_at)):
        raise LocalValidationError("SOURCE_TIME_AFTER_SNAPSHOT")


def _duplicates(values: list[str]) -> bool:
    return any(count > 1 for count in Counter(values).values())


def validate_snapshot(snapshot: dict[str, Any], metadata: dict[str, Any]) -> None:
    source_zone, snapshot_at = _validate_metadata(metadata)
    assert_no_password_fields(snapshot)
    tenant = snapshot.get("tenant")
    organizations = snapshot.get("organizations")
    staff = snapshot.get("staff")
    if not isinstance(tenant, dict):
        raise LocalValidationError("TENANT_ITEM_COUNT_INVALID")
    if not isinstance(organizations, list) or not organizations:
        raise LocalValidationError("ORGANIZATION_REQUIRED")
    if not isinstance(staff, list):
        raise LocalValidationError("STAFF_LIST_INVALID")

    legacy_tenant_id = metadata["legacyTenantId"]
    if tenant.get("tenantId") != legacy_tenant_id:
        raise LocalValidationError("TENANT_ID_MISMATCH")
    _source_id(tenant.get("participantId"), "TENANT_SOURCE_ID_REQUIRED")
    name = _required_text(tenant.get("name"), "TENANT_NAME_REQUIRED", 255)
    if len(name) > 64:
        raise LocalValidationError("TENANT_COMPANY_NAME_TOO_LONG")
    for key, maximum, code in (
        ("description", 128, "TENANT_DESCRIPTION_TOO_LONG"),
        ("contact", 32, "TENANT_CONTACTOR_TOO_LONG"),
        ("mobilePhone", 32, "TENANT_CONTACTOR_PHONE_TOO_LONG"),
        ("officePhone", 32, "TENANT_CONTACTOR_PHONE_TOO_LONG"),
        ("code", 64, "TENANT_CUSTOMER_NO_TOO_LONG"),
        ("unifiedSocialCreditCode", 32, "TENANT_USCI_TOO_LONG"),
    ):
        _bounded(tenant.get(key), maximum, code)
    if tenant.get("statusId") not in (1, 2, 3):
        raise LocalValidationError("TENANT_STATUS_INVALID")
    _validate_source_times(tenant, source_zone, snapshot_at, "TENANT_DATETIME_INVALID")

    ordered = topological_sort(organizations)
    indexed = {str(item["id"]): item for item in ordered}
    roots = [item for item in ordered if item.get("parentOrgId") in (None, 0)]
    if len(roots) != 1 or str(roots[0]["id"]) != str(tenant.get("rootOrgId")):
        raise LocalValidationError("ROOT_ORG_INVALID")
    root_ownership = roots[0].get("ownership")
    if not isinstance(root_ownership, str) or len(root_ownership) < 4:
        raise LocalValidationError("TENANT_OWNERSHIP_INVALID")
    prefix = root_ownership[:4]
    ownership_counts: Counter[str] = Counter()
    for organization in ordered:
        if organization.get("tenantId") != legacy_tenant_id:
            raise LocalValidationError("ORG_TENANT_MISMATCH")
        ownership = _required_text(organization.get("ownership"), "ORG_OWNERSHIP_REQUIRED", 20)
        if not ownership.startswith(prefix):
            raise LocalValidationError("ORG_CROSS_TENANT")
        ownership_counts[ownership] += 1
        if organization.get("deleted") is True:
            raise LocalValidationError("ORG_DELETED")
        _required_text(organization.get("name"), "ORG_NAME_REQUIRED", 128)
        if organization.get("typeId") not in range(1, 17):
            raise LocalValidationError("ORG_TYPE_INVALID")
        for key, maximum, code in (
            ("no", 32, "ORG_NO_TOO_LONG"),
            ("address", 128, "ORG_ADDRESS_TOO_LONG"),
            ("phone", 32, "ORG_PHONE_TOO_LONG"),
            ("leader", 32, "ORG_LEADER_TOO_LONG"),
        ):
            _bounded(organization.get(key), maximum, code)
        _validate_source_times(organization, source_zone, snapshot_at, "ORG_DATETIME_INVALID")
    for organization in ordered:
        data_permission = organization.get("dataPermission")
        if data_permission is not None and ownership_counts[str(data_permission)] != 1:
            raise LocalValidationError("DATA_PERMISSION_ORG_INVALID")

    staff_ids: list[str] = []
    login_names: list[str] = []
    employee_numbers: list[str] = []
    phones: list[str] = []
    for person in staff:
        source_id = _source_id(person.get("id"), "STAFF_SOURCE_ID_REQUIRED")
        staff_ids.append(source_id)
        if person.get("tenantId") != legacy_tenant_id:
            raise LocalValidationError("STAFF_TENANT_MISMATCH")
        ownership = _required_text(person.get("ownership"), "STAFF_OWNERSHIP_REQUIRED", 20)
        if not ownership.startswith(prefix):
            raise LocalValidationError("STAFF_CROSS_TENANT")
        if str(person.get("orgnizationId")) not in indexed:
            raise LocalValidationError("STAFF_ORG_SOURCE_MISSING")
        login_name = _required_text(person.get("loginName"), "STAFF_LOGIN_NAME_REQUIRED", 32)
        _required_text(person.get("name"), "STAFF_NAME_REQUIRED", 255)
        login_names.append(login_name)
        if person.get("jobNumber") is not None:
            _bounded(person["jobNumber"], 50, "STAFF_NO_TOO_LONG")
            employee_numbers.append(str(person["jobNumber"]).strip())
        if person.get("mobilePhone") is not None:
            _bounded(person["mobilePhone"], 32, "STAFF_PHONE_TOO_LONG")
            phones.append(f"{person['orgnizationId']}:{str(person['mobilePhone']).strip()}")
        for key, maximum, code in (
            ("email", 128, "STAFF_EMAIL_TOO_LONG"),
            ("headImg", 512, "STAFF_HEAD_IMG_TOO_LONG"),
            ("idCard", 32, "STAFF_ID_CARD_TOO_LONG"),
            ("jobTitle", 30, "STAFF_JOB_TITLE_TOO_LONG"),
            ("commonPlace", 500, "STAFF_COMMON_PLACE_TOO_LONG"),
            ("accountOrgNo", 20, "STAFF_ACCOUNT_ORG_NO_TOO_LONG"),
            ("affiliateAccountAppQueueNo", 30, "STAFF_QUEUE_NO_TOO_LONG"),
            ("wxUserId", 20, "STAFF_WX_USER_ID_TOO_LONG"),
            ("nailUserId", 20, "STAFF_NAIL_USER_ID_TOO_LONG"),
            ("workPhone", 64, "STAFF_TEL_TOO_LONG"),
        ):
            _bounded(person.get(key), maximum, code)
        if person.get("isUserType") not in (1, 2, 7):
            raise LocalValidationError("STAFF_USER_TYPE_INVALID")
        if person.get("statusId") not in range(1, 8):
            raise LocalValidationError("STAFF_STATUS_INVALID")
        locked = person.get("accountLockedTime")
        if locked is not None and (not isinstance(locked, int) or isinstance(locked, bool)):
            raise LocalValidationError("ACCOUNT_LOCKED_TIME_INVALID")
        _source_instant(person.get("birthday"), source_zone, "STAFF_DATETIME_INVALID")
        _validate_source_times(person, source_zone, snapshot_at, "STAFF_DATETIME_INVALID")
    if _duplicates(staff_ids):
        raise LocalValidationError("STAFF_SOURCE_ID_DUPLICATE")
    if _duplicates(login_names):
        raise LocalValidationError("STAFF_LOGIN_NAME_DUPLICATE_SOURCE")
    if _duplicates([value for value in employee_numbers if value]):
        raise LocalValidationError("STAFF_NO_DUPLICATE_SOURCE")
    if _duplicates([value for value in phones if not value.endswith(":")]):
        raise LocalValidationError("STAFF_PHONE_DUPLICATE_SOURCE")
