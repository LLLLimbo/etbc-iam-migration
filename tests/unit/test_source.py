from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import pymysql
import pytest

from etbc_migration.source import (
    EtbcReader,
    ORGANIZATION_SELECT,
    STAFF_SELECT,
    TENANT_SELECT,
    account_locked_epoch_millis,
    bit_to_bool,
    normalize_row,
)
from etbc_migration.errors import LocalValidationError


OPTIONAL_STAFF_COLUMNS = {
    "systemCode": "systemCode",
    "csremail": "email",
    "gender_id": "genderId",
    "mobilePhone": "mobilePhone",
    "headImg": "headImg",
    "identyCard": "idCard",
    "jobNumber": "jobNumber",
    "jobTitle": "jobTitle",
    "birthday": "birthday",
    "commonPlace": "commonPlace",
    "accountOrgNo": "accountOrgNo",
    "agentNo": "agentNo",
    "affiliateSubAccount_id": "affiliateSubAccountId",
    "affiliateAccountAppQueue_id": "affiliateAccountAppQueueId",
    "affiliateAccountAppQueueNo": "affiliateAccountAppQueueNo",
    "wxUserId": "wxUserId",
    "nailUserId": "nailUserId",
    "workPhone": "workPhone",
    "loginOrNot": "loginOrNot",
    "loginRetryTimes": "loginRetryTimes",
    "accountLockedTime": "accountLockedTime",
    "createDate": "createDate",
    "lastUpdateDate": "lastUpdateDate",
}
ALL_OPTIONAL_COLUMNS = {
    ("biz_participant", "iam_lessee_id"),
    ("sys_orgnization", "deleted"),
    ("sys_orgnization", "userId"),
} | {("sys_user", column) for column in OPTIONAL_STAFF_COLUMNS}


def test_queries_use_exact_etbc_columns_and_dto_aliases_without_credentials() -> None:
    assert "`tId` AS `tenantId`" in TENANT_SELECT
    assert "`createDate` AS `createDate`" in TENANT_SELECT
    assert "`parentOrg_id` AS `parentOrgId`" in ORGANIZATION_SELECT
    assert "`orgnization_id` AS `orgnizationId`" in STAFF_SELECT
    assert "`identyCard` AS `idCard`" in STAFF_SELECT
    assert "`affiliateSubAccount_id` AS `affiliateSubAccountId`" in STAFF_SELECT
    assert "`affiliateAccountAppQueue_id` AS `affiliateAccountAppQueueId`" in STAFF_SELECT
    assert "`birthday` AS `birthday`" in STAFF_SELECT
    combined = " ".join((TENANT_SELECT, ORGANIZATION_SELECT, STAFF_SELECT)).lower()
    assert "select *" not in combined
    assert "loginpwd" not in combined
    assert "multipointlogin" not in combined


@pytest.mark.parametrize(
    ("value", "expected"),
    [(b"\x00", False), (b"\x01", True), (0, False), (1, True), (False, False), (True, True), (None, None)],
)
def test_bit_to_bool(value: object, expected: bool | None) -> None:
    assert bit_to_bool(value) is expected


def test_bit_to_bool_rejects_non_boolean_bit_values() -> None:
    with pytest.raises(ValueError, match="BIT_VALUE_INVALID"):
        bit_to_bool(b"\x02")


def test_datetime_and_empty_values_are_normalized() -> None:
    row = normalize_row(
        {
            "email": "   ",
            "genderId": None,
            "loginOrNot": b"\x00",
            "createDate": datetime(2026, 7, 30, 8, 0, 0),
            "lastUpdateDate": None,
            "birthday": datetime(1990, 1, 1, 0, 0, 0),
        },
        "Asia/Shanghai",
        bit_fields={"loginOrNot"},
        datetime_fields={"birthday", "createDate", "lastUpdateDate"},
    )
    assert row == {
        "email": None,
        "genderId": None,
        "loginOrNot": False,
        "createDate": "2026-07-30T08:00:00",
        "lastUpdateDate": None,
        "birthday": "1990-01-01T00:00:00",
    }


def test_account_locked_time_uses_source_timezone_and_epoch_millis() -> None:
    assert account_locked_epoch_millis(datetime(1970, 1, 1, 8, 0, 1), "Asia/Shanghai") == 1000


def test_account_locked_time_rejects_ambiguous_local_time() -> None:
    with pytest.raises(LocalValidationError, match="SOURCE_TIMEZONE_LOCAL_TIME_AMBIGUOUS"):
        account_locked_epoch_millis(datetime(2025, 11, 2, 1, 30), "America/New_York")


class SnapshotCursor:
    def __init__(self, optional_columns: set[tuple[str, str]] | None = None) -> None:
        self.statements: list[str] = []
        self._statement = ""
        self._optional_columns = ALL_OPTIONAL_COLUMNS if optional_columns is None else optional_columns

    def __enter__(self) -> SnapshotCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, _parameters: object = None) -> None:
        missing_columns = {
            ("biz_participant", "iam_lessee_id"): "`iam_lessee_id` AS `iamLesseeId`",
            ("sys_orgnization", "deleted"): "`deleted` AS `deleted`",
            ("sys_orgnization", "userId"): "`userId` AS `userId`",
            **{
                ("sys_user", column): f"`{column}` AS `{alias}`"
                for column, alias in OPTIONAL_STAFF_COLUMNS.items()
            },
        }
        for column, sql_fragment in missing_columns.items():
            table_name, _ = column
            if (
                f"FROM `{table_name}`" in statement
                and column not in self._optional_columns
                and sql_fragment in statement
            ):
                raise AssertionError(f"query referenced unavailable column: {column}")
        self._statement = statement
        self.statements.append(statement)

    def fetchall(self) -> list[dict[str, Any]]:
        if "INFORMATION_SCHEMA" in self._statement:
            return [
                {"tableName": table_name, "columnName": column_name}
                for table_name, column_name in sorted(self._optional_columns)
            ]
        if self._statement == "SHOW GRANTS FOR CURRENT_USER()":
            return [{"grant": "GRANT ALL PRIVILEGES ON *.* TO 'operator'@'%'"}]
        if "FROM `biz_participant`" in self._statement:
            return [{"tenantId": "tenant-001", "participantId": 1, "ownership": "1001-tenant"}]
        if "FROM `sys_orgnization`" in self._statement:
            return [{"id": 10, "ownership": "1001-root", "parentOrgId": 0, "deleted": 0}]
        if "FROM `sys_user`" in self._statement:
            return []
        raise AssertionError(f"unexpected fetchall statement: {self._statement}")

    def fetchone(self) -> dict[str, datetime]:
        assert self._statement == "SELECT UTC_TIMESTAMP(6) AS `capturedAt`"
        return {"capturedAt": datetime(2026, 7, 31, 0, 0, 0)}


class SnapshotConnection:
    def __init__(self, cursor: SnapshotCursor) -> None:
        self._cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> SnapshotCursor:
        return self._cursor

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FailingOrganizationCursor(SnapshotCursor):
    def execute(self, statement: str, parameters: object = None) -> None:
        if "FROM `sys_orgnization`" in statement:
            raise pymysql.ProgrammingError(1054, "Unknown column 'obsolete' in 'field list'")
        super().execute(statement, parameters)


def test_reader_relies_on_read_only_transaction_instead_of_account_grant_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = SnapshotCursor()
    connection = SnapshotConnection(cursor)
    reader = EtbcReader(object())
    monkeypatch.setattr(reader, "_connect", lambda: connection)

    snapshot = reader.read_snapshot("tenant-001", "Asia/Shanghai")

    assert snapshot["tenant"]["tenantId"] == "tenant-001"
    assert snapshot["tenant"]["rootOrgId"] == 10
    assert snapshot["staff"] == []
    assert "SHOW GRANTS FOR CURRENT_USER()" not in cursor.statements
    assert "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY" in cursor.statements
    assert connection.rolled_back is True
    assert connection.closed is True


def test_reader_supports_legacy_schema_without_optional_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = SnapshotCursor(optional_columns=set())
    connection = SnapshotConnection(cursor)
    reader = EtbcReader(object())
    monkeypatch.setattr(reader, "_connect", lambda: connection)

    snapshot = reader.read_snapshot("tenant-001", "Asia/Shanghai")

    tenant_query = next(statement for statement in cursor.statements if "FROM `biz_participant`" in statement)
    organization_query = next(statement for statement in cursor.statements if "FROM `sys_orgnization`" in statement)
    staff_query = next(statement for statement in cursor.statements if "FROM `sys_user`" in statement)
    assert "NULL AS `iamLesseeId`" in tenant_query
    assert "NULL AS `userId`" in organization_query
    assert "FALSE AS `deleted`" in organization_query
    assert "COALESCE(`deleted`, 0)" not in organization_query
    assert "NULL AS `email`" in staff_query
    assert "NULL AS `wxUserId`" in staff_query
    assert "NULL AS `lastUpdateDate`" in staff_query
    assert "`id` AS `id`" in staff_query
    assert "`ownership` AS `ownership`" in staff_query
    assert "`orgnization_id` AS `orgnizationId`" in staff_query
    assert "`loginName` AS `loginName`" in staff_query
    assert "`name` AS `name`" in staff_query
    assert "`isUserType` AS `isUserType`" in staff_query
    assert "`status_id` AS `statusId`" in staff_query
    assert snapshot["organizations"][0]["deleted"] is False


def test_reader_supports_legacy_staff_without_wx_user_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cursor = SnapshotCursor(
        optional_columns=ALL_OPTIONAL_COLUMNS - {("sys_user", "wxUserId")}
    )
    connection = SnapshotConnection(cursor)
    reader = EtbcReader(object())
    monkeypatch.setattr(reader, "_connect", lambda: connection)

    with caplog.at_level(logging.WARNING, logger="etbc_migration.source"):
        snapshot = reader.read_snapshot("tenant-001", "Asia/Shanghai")

    staff_query = next(statement for statement in cursor.statements if "FROM `sys_user`" in statement)
    warning = next(record for record in caplog.records if "optional columns unavailable" in record.message)
    assert "NULL AS `wxUserId`" in staff_query
    assert "sys_user.wxUserId" in warning.message
    assert snapshot["staff"] == []


def test_reader_logs_database_error_context_when_snapshot_query_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    connection = SnapshotConnection(FailingOrganizationCursor())
    reader = EtbcReader(object())
    monkeypatch.setattr(reader, "_connect", lambda: connection)

    with caplog.at_level(logging.ERROR, logger="etbc_migration.source"):
        with pytest.raises(LocalValidationError, match="ETBC_SNAPSHOT_READ_FAILED"):
            reader.read_snapshot("tenant-001", "Asia/Shanghai")

    failure = next(record for record in caplog.records if "ETBC snapshot read failed" in record.message)
    assert "stage=read_organizations" in failure.message
    assert "errorType=ProgrammingError" in failure.message
    assert "errorCode=1054" in failure.message
    assert "Unknown column 'obsolete' in 'field list'" in failure.message
    assert connection.rolled_back is True
    assert connection.closed is True
