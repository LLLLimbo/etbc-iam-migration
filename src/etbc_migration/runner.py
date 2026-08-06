from __future__ import annotations

import copy
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from .errors import BatchValidationError, NetworkProtocolError, PartialFailureError, TransportError
from .state import StateStore


LOGGER = logging.getLogger(__name__)
_RETRYABLE = {"PENDING", "PROCESS_FAILED"}
_SUCCESS = {"SUCCESS", "ALREADY_EXISTS"}


class MigrationClient(Protocol):
    def import_batch(self, payload: dict[str, Any]) -> list[dict[str, Any]]: ...


class MigrationRunner:
    def __init__(
        self,
        state: StateStore,
        client: MigrationClient,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        base_delay_seconds: float = 1.0,
    ) -> None:
        self._state = state
        self._client = client
        self._sleep = sleeper
        self._base_delay = base_delay_seconds

    @staticmethod
    def _duration_ms(started: float) -> int:
        return max(0, int((time.monotonic() - started) * 1000))

    @staticmethod
    def _filtered_payload(
        original: dict[str, Any], statuses: dict[tuple[str, str], str]
    ) -> dict[str, Any] | None:
        core_retry = any(
            statuses.get((entity_type, str(item["sourceId"])), "PENDING") in _RETRYABLE
            for field, entity_type in (("tenants", "TENANT"), ("organizations", "ORGANIZATION"))
            for item in original[field]
        )
        retry_staff = [
            item
            for item in original["staff"]
            if statuses.get(("STAFF", str(item["sourceId"])), "PENDING") in _RETRYABLE
        ]
        if not core_retry and not retry_staff:
            return None
        payload = copy.deepcopy(original)
        payload["staff"] = retry_staff
        return payload

    @staticmethod
    def _process_retry_payload(payload: dict[str, Any], results: list[dict[str, Any]]) -> dict[str, Any] | None:
        failures = [item for item in results if item["status"] == "PROCESS_FAILED"]
        if not failures:
            return None
        non_staff_failure = any(item["entityType"] != "STAFF" for item in failures)
        retry = copy.deepcopy(payload)
        if not non_staff_failure:
            failed_staff_ids = {item["sourceId"] for item in failures}
            retry["staff"] = [item for item in payload["staff"] if item["sourceId"] in failed_staff_ids]
        return retry

    def run(self, batch_id: str, *, max_attempts: int = 3, replay_completed: bool = False) -> None:
        if max_attempts < 1:
            raise ValueError("MAX_ATTEMPTS_INVALID")
        shards = self._state.load_shards(batch_id)
        for shard_index, original in enumerate(shards):
            statuses = self._state.entity_statuses(batch_id)
            payload = copy.deepcopy(original) if replay_completed else self._filtered_payload(original, statuses)
            if payload is None:
                continue
            for attempt_in_run in range(max_attempts):
                LOGGER.info(
                    "IAM migration shard attempt: batchId=%s shard=%d attempt=%d staffCount=%d",
                    batch_id,
                    shard_index,
                    attempt_in_run + 1,
                    len(payload["staff"]),
                )
                started_at = self._state.mark_attempt(batch_id, shard_index, payload)
                started = time.monotonic()
                try:
                    results = self._client.import_batch(payload)
                except TransportError as error:
                    duration = self._duration_ms(started)
                    self._state.record_pending_error(batch_id, payload, error.code)
                    self._state.record_call(
                        batch_id, shard_index, started_at, duration, "TRANSPORT_ERROR", error.code
                    )
                    if attempt_in_run + 1 < max_attempts:
                        self._sleep(self._base_delay * (2**attempt_in_run))
                        continue
                    raise
                except BatchValidationError as error:
                    duration = self._duration_ms(started)
                    self._state.record_pending_error(batch_id, payload, error.code)
                    self._state.record_call(
                        batch_id, shard_index, started_at, duration, "BATCH_VALIDATION_FAILED", error.code
                    )
                    raise
                except NetworkProtocolError as error:
                    duration = self._duration_ms(started)
                    self._state.record_pending_error(batch_id, payload, error.code)
                    self._state.record_call(
                        batch_id, shard_index, started_at, duration, "PROTOCOL_ERROR", error.code
                    )
                    raise
                duration = self._duration_ms(started)
                self._state.record_results(batch_id, shard_index, duration, results)
                self._state.record_call(batch_id, shard_index, started_at, duration, "RESPONSE")
                retry_payload = self._process_retry_payload(payload, results)
                if retry_payload is None:
                    break
                payload = retry_payload
                if attempt_in_run + 1 < max_attempts:
                    self._sleep(self._base_delay * (2**attempt_in_run))

        final_statuses = [item["finalStatus"] for item in self._state.list_entities(batch_id)]
        if not final_statuses or any(status not in _SUCCESS for status in final_statuses):
            raise PartialFailureError()
