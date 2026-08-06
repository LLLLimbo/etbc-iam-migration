from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pymysql
from pymysql.cursors import DictCursor

from .errors import ConfigError, LocalValidationError


LOGGER = logging.getLogger(__name__)


TENANT_SELECT = """
SELECT
  `tId` AS `tenantId`,
  `id` AS `participantId`,
  `ownership` AS `ownership`,
  `name` AS `name`,
  `description` AS `description`,
  `contact` AS `contact`,
  `mobilePhone` AS `mobilePhone`,
  `officePhone` AS `officePhone`,
  `code` AS `code`,
  `unifiedSocialCreditCode` AS `unifiedSocialCreditCode`,
  `status_id` AS `statusId`,
  `type_id` AS `typeId`,
  `industryCode` AS `industryCode`,
  `newTenantId` AS `newTenantId`,
  `iam_lessee_id` AS `iamLesseeId`,
  `createDate` AS `createDate`,
  `lastUpdateDate` AS `lastUpdateDate`
FROM `biz_participant`
WHERE `tId` = %s
""".strip()


ORGANIZATION_SELECT = """
SELECT
  `id` AS `id`,
  `ownership` AS `ownership`,
  `parentOrg_id` AS `parentOrgId`,
  `no` AS `no`,
  `code` AS `code`,
  `name` AS `name`,
  `type_id` AS `typeId`,
  `provinceCode` AS `provinceCode`,
  `cityCode` AS `cityCode`,
  `address` AS `address`,
  `phone` AS `phone`,
  `leader` AS `leader`,
  `dataPermission` AS `dataPermission`,
  `remark` AS `remark`,
  `orgNo` AS `orgNo`,
  `systemCode` AS `systemCode`,
  `easCode` AS `easCode`,
  `addressCoordinate` AS `addressCoordinate`,
  `cisorginfo` AS `cisOrgInfo`,
  `participantCode` AS `participantCode`,
  `userId` AS `userId`,
  `deleted` AS `deleted`,
  `createDate` AS `createDate`,
  `lastUpdateDate` AS `lastUpdateDate`
FROM `sys_orgnization`
WHERE COALESCE(`deleted`, 0) = 0
  AND LEFT(`ownership`, 4) = %s
ORDER BY `id`
""".strip()


STAFF_SELECT = """
SELECT
  `id` AS `id`,
  `ownership` AS `ownership`,
  `systemCode` AS `systemCode`,
  `orgnization_id` AS `orgnizationId`,
  `loginName` AS `loginName`,
  `name` AS `name`,
  `csremail` AS `email`,
  `gender_id` AS `genderId`,
  `mobilePhone` AS `mobilePhone`,
  `headImg` AS `headImg`,
  `identyCard` AS `idCard`,
  `jobNumber` AS `jobNumber`,
  `jobTitle` AS `jobTitle`,
  `birthday` AS `birthday`,
  `commonPlace` AS `commonPlace`,
  `accountOrgNo` AS `accountOrgNo`,
  `agentNo` AS `agentNo`,
  `affiliateSubAccount_id` AS `affiliateSubAccountId`,
  `affiliateAccountAppQueue_id` AS `affiliateAccountAppQueueId`,
  `affiliateAccountAppQueueNo` AS `affiliateAccountAppQueueNo`,
  `wxUserId` AS `wxUserId`,
  `nailUserId` AS `nailUserId`,
  `workPhone` AS `workPhone`,
  `isUserType` AS `isUserType`,
  `status_id` AS `statusId`,
  `loginOrNot` AS `loginOrNot`,
  `loginRetryTimes` AS `loginRetryTimes`,
  `accountLockedTime` AS `accountLockedTime`,
  `createDate` AS `createDate`,
  `lastUpdateDate` AS `lastUpdateDate`
FROM `sys_user`
WHERE LEFT(`ownership`, 4) = %s
ORDER BY `id`
""".strip()


OPTIONAL_SOURCE_COLUMNS_SELECT = """
SELECT
  `TABLE_NAME` AS `tableName`,
  `COLUMN_NAME` AS `columnName`
FROM `INFORMATION_SCHEMA`.`COLUMNS`
WHERE `TABLE_SCHEMA` = DATABASE()
  AND (
    (`TABLE_NAME` = 'biz_participant' AND `COLUMN_NAME` = 'iam_lessee_id')
    OR (`TABLE_NAME` = 'sys_orgnization' AND `COLUMN_NAME` IN ('deleted', 'userId'))
  )
""".strip()


_OPTIONAL_SOURCE_COLUMNS = frozenset(
    {
        ("biz_participant", "iam_lessee_id"),
        ("sys_orgnization", "deleted"),
        ("sys_orgnization", "userId"),
    }
)


def _available_optional_columns(rows: list[dict[str, Any]]) -> set[tuple[str, str]]:
    available = {
        (row.get("tableName"), row.get("columnName"))
        for row in rows
        if isinstance(row.get("tableName"), str) and isinstance(row.get("columnName"), str)
    }
    return available & _OPTIONAL_SOURCE_COLUMNS


def _tenant_select(available_columns: set[tuple[str, str]]) -> str:
    if ("biz_participant", "iam_lessee_id") in available_columns:
        return TENANT_SELECT
    return TENANT_SELECT.replace("`iam_lessee_id` AS `iamLesseeId`", "NULL AS `iamLesseeId`")


def _organization_select(available_columns: set[tuple[str, str]]) -> str:
    statement = ORGANIZATION_SELECT
    if ("sys_orgnization", "userId") not in available_columns:
        statement = statement.replace("`userId` AS `userId`", "NULL AS `userId`")
    if ("sys_orgnization", "deleted") not in available_columns:
        statement = statement.replace("`deleted` AS `deleted`", "FALSE AS `deleted`")
        statement = statement.replace(
            "WHERE COALESCE(`deleted`, 0) = 0\n  AND LEFT(`ownership`, 4) = %s",
            "WHERE LEFT(`ownership`, 4) = %s",
        )
    return statement


@dataclass(frozen=True)
class SourceDatabaseConfig:
    host: str
    port: int
    user: str
    schema: str
    password: str
    connect_timeout_seconds: int = 10
    read_timeout_seconds: int = 60
    ssl_ca: str | None = None


def _zone(source_timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(source_timezone)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise LocalValidationError("SOURCE_TIMEZONE_INVALID") from error


def bit_to_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, bytes):
        numeric = int.from_bytes(value, byteorder="big", signed=False)
    elif isinstance(value, int):
        numeric = value
    else:
        raise ValueError("BIT_VALUE_INVALID")
    if numeric not in (0, 1):
        raise ValueError("BIT_VALUE_INVALID")
    return bool(numeric)


def local_datetime(value: object, source_timezone: str) -> str | None:
    if value is None:
        return None
    _zone(source_timezone)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise LocalValidationError("SOURCE_DATETIME_INVALID") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise LocalValidationError("SOURCE_DATETIME_INVALID")
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_zone(source_timezone)).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")


def account_locked_epoch_millis(value: object, source_timezone: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise LocalValidationError("ACCOUNT_LOCKED_TIME_INVALID") from error
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise LocalValidationError("ACCOUNT_LOCKED_TIME_INVALID")
    source_zone = _zone(source_timezone)
    if parsed.tzinfo is None:
        first = parsed.replace(tzinfo=source_zone, fold=0)
        second = parsed.replace(tzinfo=source_zone, fold=1)
        if first.utcoffset() != second.utcoffset():
            raise LocalValidationError("SOURCE_TIMEZONE_LOCAL_TIME_AMBIGUOUS")
        parsed = first
    else:
        parsed = parsed.astimezone(source_zone)
    return int(parsed.timestamp() * 1000)


def normalize_row(
    row: dict[str, Any],
    source_timezone: str,
    *,
    bit_fields: set[str] | frozenset[str] = frozenset(),
    datetime_fields: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, raw_value in row.items():
        if key in bit_fields:
            value = bit_to_bool(raw_value)
        elif key in datetime_fields:
            value = local_datetime(raw_value, source_timezone)
        elif isinstance(raw_value, str):
            value = raw_value.strip() or None
        else:
            value = raw_value
        normalized[key] = value
    return normalized


class EtbcReader:
    def __init__(self, config: SourceDatabaseConfig) -> None:
        self._config = config

    def _connect(self) -> pymysql.Connection:
        ssl = {"ca": self._config.ssl_ca} if self._config.ssl_ca else None
        try:
            return pymysql.connect(
                host=self._config.host,
                port=self._config.port,
                user=self._config.user,
                password=self._config.password,
                database=self._config.schema,
                charset="utf8mb4",
                autocommit=False,
                connect_timeout=self._config.connect_timeout_seconds,
                read_timeout=self._config.read_timeout_seconds,
                write_timeout=self._config.read_timeout_seconds,
                cursorclass=DictCursor,
                ssl=ssl,
            )
        except pymysql.MySQLError as error:
            raise ConfigError("ETBC_CONNECTION_FAILED") from error

    def read_snapshot(self, legacy_tenant_id: str, source_timezone: str) -> dict[str, Any]:
        if not legacy_tenant_id or not legacy_tenant_id.strip():
            raise ConfigError("LEGACY_TENANT_ID_REQUIRED")
        _zone(source_timezone)
        connection = self._connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
                cursor.execute(OPTIONAL_SOURCE_COLUMNS_SELECT)
                available_columns = _available_optional_columns(cursor.fetchall())
                cursor.execute(_tenant_select(available_columns), (legacy_tenant_id,))
                tenant_rows = cursor.fetchall()
                if len(tenant_rows) != 1:
                    raise LocalValidationError("TENANT_NOT_UNIQUE")
                tenant = normalize_row(
                    tenant_rows[0],
                    source_timezone,
                    datetime_fields={"createDate", "lastUpdateDate"},
                )
                ownership = tenant.get("ownership")
                if not isinstance(ownership, str) or len(ownership) < 4:
                    raise LocalValidationError("TENANT_OWNERSHIP_INVALID")
                initial_prefix = ownership[:4]

                cursor.execute(_organization_select(available_columns), (initial_prefix,))
                organizations = [
                    normalize_row(
                        row,
                        source_timezone,
                        bit_fields={"deleted"},
                        datetime_fields={"createDate", "lastUpdateDate"},
                    )
                    for row in cursor.fetchall()
                ]
                roots = [row for row in organizations if row.get("parentOrgId") in (None, 0)]
                if len(roots) != 1:
                    raise LocalValidationError("ROOT_ORG_NOT_UNIQUE")
                root_ownership = roots[0].get("ownership")
                if not isinstance(root_ownership, str) or len(root_ownership) < 4:
                    raise LocalValidationError("TENANT_OWNERSHIP_INVALID")
                prefix = root_ownership[:4]
                if prefix != initial_prefix:
                    raise LocalValidationError("TENANT_ROOT_OWNERSHIP_MISMATCH")

                cursor.execute(STAFF_SELECT, (prefix,))
                staff_rows = cursor.fetchall()
                staff: list[dict[str, Any]] = []
                for raw_row in staff_rows:
                    account_locked = account_locked_epoch_millis(raw_row.get("accountLockedTime"), source_timezone)
                    row = dict(raw_row)
                    row["accountLockedTime"] = account_locked
                    normalized = normalize_row(
                        row,
                        source_timezone,
                        bit_fields={"loginOrNot"},
                        datetime_fields={"birthday", "createDate", "lastUpdateDate"},
                    )
                    normalized["tenantId"] = tenant["tenantId"]
                    staff.append(normalized)

                tenant["rootOrgId"] = roots[0]["id"]
                for organization in organizations:
                    organization["tenantId"] = tenant["tenantId"]
                cursor.execute("SELECT UTC_TIMESTAMP(6) AS `capturedAt`")
                captured = cursor.fetchone()["capturedAt"]
            connection.rollback()
        except (pymysql.MySQLError, ValueError) as error:
            connection.rollback()
            raise LocalValidationError("ETBC_SNAPSHOT_READ_FAILED") from error
        finally:
            connection.close()

        captured_at = captured.replace(tzinfo=ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
        LOGGER.info(
            "ETBC snapshot captured: tenantCount=1 organizationCount=%d staffCount=%d",
            len(organizations),
            len(staff),
        )
        return {
            "tenant": tenant,
            "organizations": organizations,
            "staff": staff,
            "captured_at": captured_at,
        }
