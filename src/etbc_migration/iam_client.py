from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import requests

from .errors import BatchValidationError, NetworkProtocolError, TransportError


_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
_FIELDS = (("tenants", "TENANT"), ("organizations", "ORGANIZATION"), ("staff", "STAFF"))
_STATUSES = {"SUCCESS", "ALREADY_EXISTS", "VALIDATION_FAILED", "PROCESS_FAILED"}


def _safe_code(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and _SAFE_CODE.fullmatch(value) else fallback


def _expected_items(payload: dict[str, Any]) -> set[tuple[str, str, str]]:
    expected: set[tuple[str, str, str]] = set()
    for field, entity_type in _FIELDS:
        items = payload.get(field)
        if not isinstance(items, list):
            raise NetworkProtocolError("IAM_REQUEST_SHAPE_INVALID")
        for item in items:
            key = (entity_type, str(item.get("correlationId")), str(item.get("sourceId")))
            if key in expected:
                raise NetworkProtocolError("IAM_REQUEST_ITEM_DUPLICATE")
            expected.add(key)
    return expected


def validate_response(payload: dict[str, Any], envelope: object) -> list[dict[str, Any]]:
    if not isinstance(envelope, dict):
        raise NetworkProtocolError("IAM_ENVELOPE_INVALID")
    code = envelope.get("code")
    if code == 599:
        raise BatchValidationError(_safe_code(envelope.get("message"), "MIGRATION_BATCH_VALIDATION_FAILED"))
    if not isinstance(code, int) or isinstance(code, bool) or code != 0:
        raise NetworkProtocolError("IAM_ENVELOPE_CODE_INVALID")
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise NetworkProtocolError("IAM_RESPONSE_DATA_INVALID")
    if data.get("migrationBatchId") != payload.get("migrationBatchId"):
        raise NetworkProtocolError("IAM_RESPONSE_METADATA_MISMATCH")
    if data.get("legacyTenantId") != payload.get("legacyTenantId"):
        raise NetworkProtocolError("IAM_RESPONSE_METADATA_MISMATCH")
    response_modules = data.get("enabledModules")
    if (
        not isinstance(response_modules, list)
        or any(not isinstance(module, str) for module in response_modules)
        or len(set(response_modules)) != len(response_modules)
        or set(response_modules) != set(payload.get("enabledModules", []))
    ):
        raise NetworkProtocolError("IAM_RESPONSE_METADATA_MISMATCH")
    raw_items = data.get("items")
    if not isinstance(raw_items, list):
        raise NetworkProtocolError("IAM_RESULT_ITEMS_INVALID")
    actual: set[tuple[str, str, str]] = set()
    validated: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise NetworkProtocolError("IAM_RESULT_ITEM_INVALID")
        key = (
            str(raw_item.get("entityType")),
            str(raw_item.get("correlationId")),
            str(raw_item.get("sourceId")),
        )
        if key in actual:
            raise NetworkProtocolError("IAM_RESULT_ITEM_DUPLICATE")
        actual.add(key)
        status = raw_item.get("status")
        if status not in _STATUSES:
            raise NetworkProtocolError("IAM_RESULT_STATUS_INVALID")
        target_id = raw_item.get("targetId")
        if status in {"SUCCESS", "ALREADY_EXISTS"} and (target_id is None or str(target_id) == ""):
            raise NetworkProtocolError("IAM_RESULT_TARGET_ID_REQUIRED")
        error_code = raw_item.get("errorCode")
        if error_code is not None and _safe_code(error_code, "") == "":
            raise NetworkProtocolError("IAM_RESULT_ERROR_CODE_INVALID")
        audit_codes = raw_item.get("auditCodes") or []
        if not isinstance(audit_codes, list) or any(_safe_code(item, "") == "" for item in audit_codes):
            raise NetworkProtocolError("IAM_RESULT_AUDIT_CODES_INVALID")
        validated.append(
            {
                "entityType": key[0],
                "correlationId": key[1],
                "sourceId": key[2],
                "targetId": None if target_id is None else str(target_id),
                "status": status,
                "errorCode": error_code,
                "auditCodes": list(audit_codes),
            }
        )
    if actual != _expected_items(payload):
        raise NetworkProtocolError("IAM_RESULT_ITEM_SET_MISMATCH")
    return validated


@dataclass(frozen=True)
class IamClientConfig:
    base_url: str
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 60.0


class IamClient:
    def __init__(self, config: IamClientConfig, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()
        self._url = config.base_url.rstrip("/") + "/inter/iam/mgmt/migration/v1/batches/import"

    def import_batch(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            response = self._session.post(
                self._url,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=(self._config.connect_timeout_seconds, self._config.read_timeout_seconds),
            )
        except requests.RequestException as error:
            raise TransportError("IAM_TRANSPORT_ERROR") from error
        if response.status_code >= 500:
            raise TransportError("IAM_HTTP_SERVER_ERROR")
        if response.status_code != 200:
            raise NetworkProtocolError("IAM_HTTP_STATUS_INVALID")
        try:
            envelope = response.json()
        except requests.exceptions.JSONDecodeError as error:
            raise NetworkProtocolError("IAM_RESPONSE_JSON_INVALID") from error
        return validate_response(payload, envelope)
