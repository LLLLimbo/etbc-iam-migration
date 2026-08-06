from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pymysql
import pytest
import requests

from etbc_migration.config import load_config
from etbc_migration.state import StateStore
from etbc_migration.web import WebConsole, create_server


pytestmark = pytest.mark.integration

CONFIG = "/app/integration/config.toml"
STATE_DIR = Path("/state")
BATCH_ID = "python-compose-batch-001"
LEGACY_TENANT_ID = "synthetic-tenant-001"
CORE_MODULES = "TENANT,ORGANIZATION,STAFF"


def wait_http(url: str) -> None:
    for _ in range(120):
        try:
            if requests.get(url, timeout=2).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    pytest.fail("service readiness timeout")


def run_cli(arguments: list[str], expected: int) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [sys.executable, "-m", "etbc_migration", "--config", CONFIG, *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ.copy(),
        timeout=300,
    )
    assert completed.returncode == expected, (
        f"unexpected CLI exit {completed.returncode}; stdout={completed.stdout!r}; "
        f"stderr={completed.stderr!r}"
    )
    return completed


def iam_connection() -> pymysql.Connection:
    return pymysql.connect(
        host="iam-db",
        port=3306,
        user="root",
        password=os.environ["IAM_DB_ROOT_PASSWORD"],
        database="iam_mgmt",
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def scalar(connection: pymysql.Connection, statement: str, parameters: tuple = ()) -> object:
    with connection.cursor() as cursor:
        cursor.execute(statement, parameters)
        row = cursor.fetchone()
        return next(iter(row.values()))


def optional_snapshot(connection: pymysql.Connection) -> tuple[int, ...]:
    tables = (
        "iam_application",
        "iam_resource",
        "iam_permission",
        "iam_feature",
        "iam_role_tenant",
        "iam_user_role",
        "iam_position",
        "iam_user_staff_position",
        "legacy_resource_mapping",
        "legacy_role_mapping",
    )
    return tuple(int(scalar(connection, f"SELECT COUNT(*) FROM `{table}`")) for table in tables)


def create_selective_mapping_failure(connection: pymysql.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS python_migration_fail_user_mapping")
        cursor.execute(
            """
            CREATE TRIGGER python_migration_fail_user_mapping
            BEFORE INSERT ON legacy_user_mapping
            FOR EACH ROW
            BEGIN
              IF NEW.legacy_user_id = 101 THEN
                SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'synthetic mapping failure';
              END IF;
            END
            """
        )


def drop_mapping_failure(connection: pymysql.Connection) -> None:
    with connection.cursor() as cursor:
        cursor.execute("DROP TRIGGER IF EXISTS python_migration_fail_user_mapping")


def verify_web_console(snapshot_at: str) -> None:
    csrf_token = "integration-web-csrf-token"
    console = WebConsole(
        config_path=CONFIG,
        state_dir=STATE_DIR,
        config=load_config(CONFIG),
        csrf_token=csrf_token,
    )
    server = create_server(console, "0.0.0.0", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    browser = requests.Session()
    try:
        home = browser.get(base_url, timeout=10)
        assert home.status_code == 200
        assert home.headers["Content-Security-Policy"].startswith("default-src 'self'")
        assert "SameSite=Strict" in home.headers["Set-Cookie"]
        assert f'value="{csrf_token}"' in home.text
        assert os.environ["ETBC_PASSWORD"] not in home.text
        assert "synthetic_admin@example.com" not in home.text
        console.csrf_token = "integration-web-rotated-token"

        preflight = browser.post(
            f"{base_url}/actions/preflight",
            data={
                "csrfToken": csrf_token,
                "legacyTenantId": LEGACY_TENANT_ID,
                "sourceTimezone": "Asia/Shanghai",
            },
            allow_redirects=False,
            timeout=300,
        )
        assert preflight.status_code == 303
        preflight_result = browser.get(f"{base_url}{preflight.headers['Location']}", timeout=10)
        assert preflight_result.status_code == 200
        assert "PREFLIGHT_OK" in preflight_result.text

        replay = browser.post(
            f"{base_url}/actions/migrate",
            data={
                "csrfToken": csrf_token,
                "legacyTenantId": LEGACY_TENANT_ID,
                "batchId": BATCH_ID,
                "sourceTimezone": "Asia/Shanghai",
                "snapshotAt": snapshot_at,
                "staffChunkSize": "150",
                "maxAttempts": "3",
                "confirmWrite": "confirmed",
            },
            allow_redirects=False,
            timeout=300,
        )
        assert replay.status_code == 303
        replay_result = browser.get(f"{base_url}{replay.headers['Location']}", timeout=10)
        assert replay_result.status_code == 200
        assert "SUCCESS" in replay_result.text
        assert BATCH_ID in replay_result.text

        batch = browser.get(f"{base_url}/batches", params={"batchId": BATCH_ID}, timeout=10)
        assert batch.status_code == 200
        assert "ALREADY_EXISTS" in batch.text
        assert "synthetic_admin@example.com" not in batch.text
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_end_to_end_migration_resume_idempotency_and_security() -> None:
    wait_http("http://iam-management:17020/v3/api-docs")
    wait_http("http://iam-auth-center:17021/v3/api-docs")
    wait_http("http://iam-proxy:18080/health")
    connection = iam_connection()
    baseline = optional_snapshot(connection)
    snapshot_at = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
    common = [
        "--batch-id",
        BATCH_ID,
        "--legacy-tenant-id",
        LEGACY_TENANT_ID,
        "--enabled-modules",
        CORE_MODULES,
        "--source-timezone",
        "Asia/Shanghai",
        "--snapshot-at",
        snapshot_at,
        "--state-dir",
        str(STATE_DIR),
    ]

    run_cli(
        [
            "preflight",
            "--legacy-tenant-id",
            LEGACY_TENANT_ID,
            "--enabled-modules",
            CORE_MODULES,
            "--source-timezone",
            "Asia/Shanghai",
        ],
        0,
    )

    create_selective_mapping_failure(connection)
    run_cli(["migrate", *common], 3)
    with StateStore(STATE_DIR) as store:
        partial = {
            (item["entityType"], item["sourceId"]): item
            for item in store.list_entities(BATCH_ID)
        }
    assert partial[("STAFF", "100")]["finalStatus"] == "SUCCESS"
    assert partial[("STAFF", "101")]["finalStatus"] == "PROCESS_FAILED"
    assert partial[("STAFF", "101")]["attemptCount"] == 3

    drop_mapping_failure(connection)
    run_cli(["resume", "--batch-id", BATCH_ID, "--state-dir", str(STATE_DIR)], 0)

    assert scalar(
        connection,
        "SELECT COUNT(*) FROM legacy_tenant_mapping WHERE legacy_tenant_id = %s",
        (LEGACY_TENANT_ID,),
    ) == 1
    assert scalar(connection, "SELECT COUNT(*) FROM legacy_org_mapping WHERE legacy_org_id IN (10, 11)") == 2
    assert scalar(connection, "SELECT COUNT(*) FROM legacy_user_mapping WHERE legacy_user_id IN (100, 101)") == 2

    root_target = scalar(connection, "SELECT iam_org_id FROM legacy_org_mapping WHERE legacy_org_id = 10")
    child_target = scalar(connection, "SELECT iam_org_id FROM legacy_org_mapping WHERE legacy_org_id = 11")
    assert scalar(connection, "SELECT parent_id FROM iam_organization WHERE id = %s", (child_target,)) == root_target
    admin_target = scalar(connection, "SELECT iam_user_id FROM legacy_user_mapping WHERE legacy_user_id = 100")
    female_target = scalar(connection, "SELECT iam_user_id FROM legacy_user_mapping WHERE legacy_user_id = 101")
    assert scalar(connection, "SELECT org_id FROM iam_user_staff WHERE user_id = %s", (admin_target,)) == child_target
    assert scalar(connection, "SELECT email FROM iam_user WHERE id = %s", (admin_target,)) == "synthetic_admin@example.com"
    assert scalar(connection, "SELECT gender FROM iam_user WHERE id = %s", (admin_target,)) == "M"
    assert scalar(connection, "SELECT gender FROM iam_user WHERE id = %s", (female_target,)) == "F"
    assert scalar(connection, "SELECT is_admin FROM iam_user_staff WHERE user_id = %s", (admin_target,)) == 1
    assert scalar(connection, "SELECT algorithm FROM iam_user WHERE id = %s", (admin_target,)) == "BCRYPT"
    assert scalar(connection, "SELECT password FROM iam_user WHERE id = %s", (admin_target,)) != "P@ssword123456"
    assert scalar(
        connection,
        "SELECT COUNT(*) FROM iam_tenant_ext "
        "WHERE key_ LIKE 'ETBC_MA_STF_%%' "
        "AND JSON_UNQUOTE(JSON_EXTRACT(value, '$.unmapped.jobTitle')) = %s",
        ("Synthetic Operator",),
    ) == 1

    password_hash_before = scalar(connection, "SELECT SHA2(password, 256) FROM iam_user WHERE id = %s", (admin_target,))
    run_cli(["migrate", *common], 0)
    with StateStore(STATE_DIR) as store:
        rerun = store.list_entities(BATCH_ID)
    assert all(item["finalStatus"] == "ALREADY_EXISTS" for item in rerun)
    assert scalar(connection, "SELECT SHA2(password, 256) FROM iam_user WHERE id = %s", (admin_target,)) == password_hash_before

    verify_web_console(snapshot_at)
    with StateStore(STATE_DIR) as store:
        web_rerun = store.list_entities(BATCH_ID)
    assert all(item["finalStatus"] == "ALREADY_EXISTS" for item in web_rerun)

    login_response = requests.post(
        "http://iam-auth-center:17021/api/iam/ac/public/v2/auth/tenant/app/login",
        headers={"Content-Type": "application/json", "X-Request-ID": "python-migration-it"},
        json={"loginName": "synthetic_admin", "password": "P@ssword123456"},
        timeout=30,
    )
    assert login_response.status_code == 200
    assert login_response.json().get("code") == 0

    run_cli(["verify", "--batch-id", BATCH_ID, "--state-dir", str(STATE_DIR)], 0)
    report_files = list((STATE_DIR / "reports").glob("migration-report-*.json"))
    assert len(report_files) == 1
    report = json.loads(report_files[0].read_text(encoding="utf-8"))
    assert report["metadata"]["enabledModules"] == ["TENANT", "ORGANIZATION", "STAFF"]
    assert report["sourceSnapshot"]["staffCount"] == 2

    observations = [
        json.loads(line)
        for line in Path("/observations/requests.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert observations
    assert all(item["forbiddenPasswordField"] is False for item in observations)
    assert all(item["legacyAuthenticationHeadersPresent"] is False for item in observations)
    assert optional_snapshot(connection) == baseline
    connection.close()
