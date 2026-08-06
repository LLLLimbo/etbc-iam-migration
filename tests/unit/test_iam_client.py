from __future__ import annotations

import copy
from typing import Any

import pytest

from etbc_migration.cli import _iam_config
from etbc_migration.errors import BatchValidationError, NetworkProtocolError
from etbc_migration.iam_client import IamClient, IamClientConfig, validate_response
from etbc_migration.payloads import build_shards


def success_response(payload: dict) -> dict:
    items = []
    for field, entity_type in (
        ("tenants", "TENANT"),
        ("organizations", "ORGANIZATION"),
        ("staff", "STAFF"),
    ):
        items.extend(
            {
                "entityType": entity_type,
                "correlationId": item["correlationId"],
                "sourceId": item["sourceId"],
                "targetId": f"target-{item['sourceId']}",
                "status": "SUCCESS",
                "errorCode": None,
                "auditCodes": [],
            }
            for item in payload[field]
        )
    return {
        "code": 0,
        "data": {
            "migrationBatchId": payload["migrationBatchId"],
            "legacyTenantId": payload["legacyTenantId"],
            "enabledModules": payload["enabledModules"],
            "items": items,
        },
    }


def test_outer_code_599_is_batch_validation_failure(source_snapshot: dict, metadata: dict) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    with pytest.raises(BatchValidationError, match="MIGRATION_REQUEST_INVALID"):
        validate_response(payload, {"code": 599, "message": "MIGRATION_REQUEST_INVALID"})


@pytest.mark.parametrize("code", [1, 500, "0", None])
def test_unknown_outer_code_is_protocol_error(source_snapshot: dict, metadata: dict, code: object) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    with pytest.raises(NetworkProtocolError, match="IAM_ENVELOPE_CODE_INVALID"):
        validate_response(payload, {"code": code})


def test_response_items_must_match_request_one_to_one(source_snapshot: dict, metadata: dict) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    response = success_response(payload)
    response["data"]["items"].pop()
    with pytest.raises(NetworkProtocolError, match="IAM_RESULT_ITEM_SET_MISMATCH"):
        validate_response(payload, response)

    response = success_response(payload)
    response["data"]["items"].append(copy.deepcopy(response["data"]["items"][0]))
    with pytest.raises(NetworkProtocolError, match="IAM_RESULT_ITEM_DUPLICATE"):
        validate_response(payload, response)

    response = success_response(payload)
    response["data"]["items"][0]["sourceId"] = "unknown"
    with pytest.raises(NetworkProtocolError, match="IAM_RESULT_ITEM_SET_MISMATCH"):
        validate_response(payload, response)


def test_valid_success_response_is_returned(source_snapshot: dict, metadata: dict) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    assert len(validate_response(payload, success_response(payload))) == 6


def test_malformed_response_modules_are_controlled_protocol_error(
    source_snapshot: dict, metadata: dict
) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    response = success_response(payload)
    response["data"]["enabledModules"] = [["TENANT"]]
    with pytest.raises(NetworkProtocolError, match="IAM_RESPONSE_METADATA_MISMATCH"):
        validate_response(payload, response)


class _SuccessfulResponse:
    status_code = 200

    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope

    def json(self) -> dict[str, Any]:
        return self._envelope


class _CapturingSession:
    def __init__(self, envelope: dict[str, Any]) -> None:
        self._envelope = envelope
        self.request: dict[str, Any] | None = None

    def post(self, url: str, **kwargs: Any) -> _SuccessfulResponse:
        self.request = {"url": url, **kwargs}
        return _SuccessfulResponse(self._envelope)


def test_iam_client_uses_current_unauthenticated_endpoint_contract(
    source_snapshot: dict,
    metadata: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = build_shards(source_snapshot, metadata, 150)[0]
    session = _CapturingSession(success_response(payload))
    monkeypatch.setenv("IAM_MIGRATION_INTERNAL_AUTH_TOKEN", "obsolete-token-must-be-ignored")

    results = IamClient(IamClientConfig(base_url="https://iam.example.test"), session).import_batch(payload)

    assert len(results) == 6
    assert session.request is not None
    assert session.request["headers"] == {"Content-Type": "application/json"}
    assert "X-Iam-Internal-Token" not in session.request["headers"]
    assert "X-Iam-Internal-Caller" not in session.request["headers"]


def test_cli_iam_config_does_not_require_obsolete_token_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("IAM_MIGRATION_INTERNAL_AUTH_TOKEN", raising=False)

    result = _iam_config(
        {"iam": {"base_url": "https://iam.example.test"}},
        None,
    )

    assert result == IamClientConfig(base_url="https://iam.example.test")
