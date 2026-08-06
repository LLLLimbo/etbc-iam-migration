from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import ConfigError, LocalValidationError
from .validation import assert_no_password_fields


_ENTITY_FIELDS = (("tenants", "TENANT"), ("organizations", "ORGANIZATION"), ("staff", "STAFF"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        requested_state_dir = Path(state_dir).expanduser()
        if requested_state_dir.is_symlink():
            raise ConfigError("STATE_DIRECTORY_SYMLINK_FORBIDDEN")
        self.state_dir = requested_state_dir.resolve()
        old_umask = os.umask(0o077)
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.state_dir.chmod(0o700)
            self.path = self.state_dir / "migration-state.sqlite3"
            if self.path.is_symlink():
                raise ConfigError("STATE_DATABASE_SYMLINK_FORBIDDEN")
            self._connection = sqlite3.connect(self.path)
        except ConfigError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise ConfigError("STATE_DIRECTORY_INVALID") from error
        finally:
            os.umask(old_umask)
        self.path.chmod(0o600)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._initialize()

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS batches (
              batch_id TEXT PRIMARY KEY,
              metadata_json TEXT NOT NULL,
              source_summary_json TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shards (
              batch_id TEXT NOT NULL,
              shard_index INTEGER NOT NULL,
              payload_json TEXT NOT NULL,
              PRIMARY KEY (batch_id, shard_index),
              FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
            );
            CREATE TABLE IF NOT EXISTS entities (
              batch_id TEXT NOT NULL,
              entity_type TEXT NOT NULL,
              source_id TEXT NOT NULL,
              correlation_id TEXT NOT NULL,
              first_shard_index INTEGER NOT NULL,
              last_shard_index INTEGER,
              target_id TEXT,
              attempt_count INTEGER NOT NULL DEFAULT 0,
              final_status TEXT NOT NULL DEFAULT 'PENDING',
              error_code TEXT,
              audit_codes_json TEXT NOT NULL DEFAULT '[]',
              first_attempt_at TEXT,
              last_attempt_at TEXT,
              last_duration_ms INTEGER,
              PRIMARY KEY (batch_id, entity_type, source_id),
              UNIQUE (batch_id, correlation_id),
              FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
            );
            CREATE TABLE IF NOT EXISTS calls (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id TEXT NOT NULL,
              shard_index INTEGER NOT NULL,
              started_at TEXT NOT NULL,
              ended_at TEXT NOT NULL,
              duration_ms INTEGER NOT NULL,
              outcome TEXT NOT NULL,
              error_code TEXT,
              FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
            );
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def create_snapshot(
        self,
        metadata: dict[str, Any],
        shards: list[dict[str, Any]],
        source_summary: dict[str, Any],
    ) -> bool:
        assert_no_password_fields(shards)
        assert_no_password_fields(source_summary)
        batch_id = str(metadata.get("migrationBatchId") or "")
        if not batch_id:
            raise LocalValidationError("MIGRATION_BATCH_ID_REQUIRED")
        metadata_json = _json(metadata)
        source_json = _json(source_summary)
        existing = self._connection.execute(
            "SELECT metadata_json, source_summary_json FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if existing:
            if existing["metadata_json"] != metadata_json:
                raise LocalValidationError("MIGRATION_BATCH_METADATA_MISMATCH")
            if existing["source_summary_json"] != source_json:
                raise LocalValidationError("MIGRATION_SNAPSHOT_MISMATCH")
            persisted = [_json(item) for item in self.load_shards(batch_id)]
            if persisted != [_json(item) for item in shards]:
                raise LocalValidationError("MIGRATION_SNAPSHOT_MISMATCH")
            return False
        now = _now()
        with self._connection:
            self._connection.execute(
                "INSERT INTO batches(batch_id, metadata_json, source_summary_json, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, metadata_json, source_json, now, now),
            )
            for shard_index, payload in enumerate(shards):
                self._connection.execute(
                    "INSERT INTO shards(batch_id, shard_index, payload_json) VALUES (?, ?, ?)",
                    (batch_id, shard_index, _json(payload)),
                )
                for field, entity_type in _ENTITY_FIELDS:
                    for item in payload[field]:
                        self._connection.execute(
                            "INSERT OR IGNORE INTO entities("
                            "batch_id, entity_type, source_id, correlation_id, first_shard_index"
                            ") VALUES (?, ?, ?, ?, ?)",
                            (
                                batch_id,
                                entity_type,
                                str(item["sourceId"]),
                                str(item["correlationId"]),
                                shard_index,
                            ),
                        )
        self.path.chmod(0o600)
        return True

    def load_metadata(self, batch_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT metadata_json FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise ConfigError("MIGRATION_BATCH_STATE_NOT_FOUND")
        return json.loads(row["metadata_json"])

    def has_batch(self, batch_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,)
            ).fetchone()
            is not None
        )

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT metadata_json, created_at, updated_at FROM batches ORDER BY updated_at DESC"
        ).fetchall()
        batches: list[dict[str, Any]] = []
        for row in rows:
            metadata = json.loads(row["metadata_json"])
            batches.append(
                {
                    "batchId": metadata["migrationBatchId"],
                    "legacyTenantId": metadata["legacyTenantId"],
                    "snapshotAt": metadata["snapshotAt"],
                    "createdAt": row["created_at"],
                    "updatedAt": row["updated_at"],
                }
            )
        return batches

    def load_source_summary(self, batch_id: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT source_summary_json FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise ConfigError("MIGRATION_BATCH_STATE_NOT_FOUND")
        return json.loads(row["source_summary_json"])

    def load_shards(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT payload_json FROM shards WHERE batch_id = ? ORDER BY shard_index", (batch_id,)
        ).fetchall()
        if not rows:
            raise ConfigError("MIGRATION_BATCH_STATE_NOT_FOUND")
        return [json.loads(row["payload_json"]) for row in rows]

    @staticmethod
    def payload_keys(payload: dict[str, Any]) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        for field, entity_type in _ENTITY_FIELDS:
            keys.extend((entity_type, str(item["sourceId"])) for item in payload[field])
        return keys

    def mark_attempt(self, batch_id: str, shard_index: int, payload: dict[str, Any]) -> str:
        started_at = _now()
        with self._connection:
            for entity_type, source_id in self.payload_keys(payload):
                updated = self._connection.execute(
                    "UPDATE entities SET attempt_count = attempt_count + 1, "
                    "first_attempt_at = COALESCE(first_attempt_at, ?), last_attempt_at = ?, "
                    "last_shard_index = ? WHERE batch_id = ? AND entity_type = ? AND source_id = ?",
                    (started_at, started_at, shard_index, batch_id, entity_type, source_id),
                )
                if updated.rowcount != 1:
                    raise ConfigError("STATE_ENTITY_NOT_FOUND")
        return started_at

    def record_results(
        self, batch_id: str, shard_index: int, duration_ms: int, results: list[dict[str, Any]]
    ) -> None:
        with self._connection:
            for item in results:
                updated = self._connection.execute(
                    "UPDATE entities SET target_id = ?, final_status = ?, error_code = ?, "
                    "audit_codes_json = ?, last_shard_index = ?, last_duration_ms = ?, last_attempt_at = ? "
                    "WHERE batch_id = ? AND entity_type = ? AND source_id = ? AND correlation_id = ?",
                    (
                        item.get("targetId"),
                        item["status"],
                        item.get("errorCode"),
                        _json(item.get("auditCodes") or []),
                        shard_index,
                        duration_ms,
                        _now(),
                        batch_id,
                        item["entityType"],
                        item["sourceId"],
                        item["correlationId"],
                    ),
                )
                if updated.rowcount != 1:
                    raise ConfigError("STATE_RESULT_ITEM_UNKNOWN")
            self._connection.execute("UPDATE batches SET updated_at = ? WHERE batch_id = ?", (_now(), batch_id))

    def record_pending_error(self, batch_id: str, payload: dict[str, Any], error_code: str) -> None:
        terminal = ("SUCCESS", "ALREADY_EXISTS", "VALIDATION_FAILED")
        with self._connection:
            for entity_type, source_id in self.payload_keys(payload):
                self._connection.execute(
                    "UPDATE entities SET error_code = ? WHERE batch_id = ? AND entity_type = ? AND source_id = ? "
                    "AND final_status NOT IN (?, ?, ?)",
                    (error_code, batch_id, entity_type, source_id, *terminal),
                )

    def record_call(
        self,
        batch_id: str,
        shard_index: int,
        started_at: str,
        duration_ms: int,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        with self._connection:
            self._connection.execute(
                "INSERT INTO calls(batch_id, shard_index, started_at, ended_at, duration_ms, outcome, error_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, shard_index, started_at, _now(), duration_ms, outcome, error_code),
            )

    def entity_statuses(self, batch_id: str) -> dict[tuple[str, str], str]:
        rows = self._connection.execute(
            "SELECT entity_type, source_id, final_status FROM entities WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        return {(row["entity_type"], row["source_id"]): row["final_status"] for row in rows}

    def list_entities(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT entity_type, source_id, correlation_id, target_id, attempt_count, final_status, "
            "error_code, audit_codes_json, first_shard_index, last_shard_index, first_attempt_at, "
            "last_attempt_at, last_duration_ms FROM entities WHERE batch_id = ? "
            "ORDER BY CASE entity_type WHEN 'TENANT' THEN 1 WHEN 'ORGANIZATION' THEN 2 ELSE 3 END, source_id",
            (batch_id,),
        ).fetchall()
        return [
            {
                "entityType": row["entity_type"],
                "sourceId": row["source_id"],
                "correlationId": row["correlation_id"],
                "targetId": row["target_id"],
                "attemptCount": row["attempt_count"],
                "finalStatus": row["final_status"],
                "errorCode": row["error_code"],
                "auditCodes": json.loads(row["audit_codes_json"]),
                "firstShardIndex": row["first_shard_index"],
                "lastShardIndex": row["last_shard_index"],
                "firstAttemptAt": row["first_attempt_at"],
                "lastAttemptAt": row["last_attempt_at"],
                "lastDurationMs": row["last_duration_ms"],
            }
            for row in rows
        ]

    def list_calls(self, batch_id: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            "SELECT shard_index, started_at, ended_at, duration_ms, outcome, error_code "
            "FROM calls WHERE batch_id = ? ORDER BY id",
            (batch_id,),
        ).fetchall()
        return [dict(row) for row in rows]
