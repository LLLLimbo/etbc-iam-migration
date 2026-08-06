from __future__ import annotations

import http.client
import json
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import pytest

from etbc_migration import cli
from etbc_migration.errors import ConfigError
from etbc_migration.web import (
    CliExecutor,
    CommandResult,
    WebConsole,
    create_server,
    validate_web_bind,
)


class FakeExecutor:
    def __init__(self, result: CommandResult | None = None) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.result = result or CommandResult(
            action="preflight",
            exit_code=0,
            payload={"status": "PREFLIGHT_OK", "tenantCount": 1, "staffCount": 3},
            error_code=None,
        )

    def execute(self, action: str, values: dict[str, str]) -> CommandResult:
        self.calls.append((action, values))
        return self.result


@contextmanager
def running_console(console: WebConsole, bind: str = "127.0.0.1"):
    server = create_server(console, bind, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(
    port: int,
    method: str,
    path: str,
    fields: dict[str, str] | None = None,
    request_headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], str]:
    body = urlencode(fields or {})
    headers = {"Content-Type": "application/x-www-form-urlencoded"} if fields is not None else {}
    headers.update(request_headers or {})
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request(method, path, body=body if fields is not None else None, headers=headers)
    response = connection.getresponse()
    content = response.read().decode("utf-8")
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    connection.close()
    return response.status, response_headers, content


def console(
    tmp_path: Path,
    executor: FakeExecutor | None = None,
    csrf_token: str = "test-csrf-token",
) -> WebConsole:
    return WebConsole(
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        config={"migration": {"staff_chunk_size": 150, "max_attempts": 3}},
        executor=executor or FakeExecutor(),
        csrf_token=csrf_token,
    )


def test_home_is_self_contained_and_never_renders_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETBC_PASSWORD", "source-secret-never-render")
    monkeypatch.setenv("IAM_MIGRATION_INTERNAL_AUTH_TOKEN", "iam-secret-never-render")
    with running_console(console(tmp_path)) as port:
        status, headers, content = request(port, "GET", "/")

    assert status == 200
    assert "ETBC → IAM 迁移控制台" in content
    assert "TENANT · ORGANIZATION · STAFF" in content
    assert "source-secret-never-render" not in content
    assert "iam-secret-never-render" not in content
    assert "IAM 内部令牌" not in content
    assert "IAM 接口地址" in content
    assert "default-src 'self'" in headers["content-security-policy"]
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert "https://" not in content


def test_health_check_contains_no_configuration(tmp_path: Path) -> None:
    with running_console(console(tmp_path)) as port:
        status, headers, content = request(port, "GET", "/healthz")

    assert status == 200
    assert headers["content-type"].startswith("application/json")
    assert json.loads(content) == {"status": "ok"}


def test_post_rejects_missing_csrf_token(tmp_path: Path) -> None:
    executor = FakeExecutor()
    with running_console(console(tmp_path, executor)) as port:
        status, _, content = request(
            port,
            "POST",
            "/actions/preflight",
            {"legacyTenantId": "tenant-001", "sourceTimezone": "Asia/Shanghai"},
        )

    assert status == 403
    assert "页面校验信息已更新" in content
    assert executor.calls == []


def test_migrate_requires_deliberate_write_confirmation(tmp_path: Path) -> None:
    executor = FakeExecutor()
    fields = {
        "csrfToken": "test-csrf-token",
        "legacyTenantId": "tenant-001",
        "batchId": "batch-web-001",
        "sourceTimezone": "Asia/Shanghai",
        "snapshotAt": "2026-07-31T12:00:00Z",
    }
    with running_console(console(tmp_path, executor)) as port:
        status, _, content = request(port, "POST", "/actions/migrate", fields)

    assert status == 400
    assert "确认写入 IAM" in content
    assert executor.calls == []


def test_confirmed_migrate_uses_only_validated_form_fields(tmp_path: Path) -> None:
    executor = FakeExecutor(
        CommandResult(
            action="migrate",
            exit_code=0,
            payload={"status": "SUCCESS", "batchId": "batch-web-001"},
            error_code=None,
        )
    )
    fields = {
        "csrfToken": "test-csrf-token",
        "legacyTenantId": "tenant-001",
        "batchId": "batch-web-001",
        "sourceTimezone": "Asia/Shanghai",
        "snapshotAt": "2026-07-31T12:00:00Z",
        "staffChunkSize": "100",
        "maxAttempts": "3",
        "confirmWrite": "confirmed",
        "enabledModules": "TENANT,PRODUCT",
        "unexpected": "ignored",
    }
    with running_console(console(tmp_path, executor)) as port:
        status, headers, _ = request(port, "POST", "/actions/migrate", fields)

    assert status == 303
    assert headers["location"].startswith("/results/")
    assert executor.calls == [
        (
            "migrate",
            {
                "legacy_tenant_id": "tenant-001",
                "batch_id": "batch-web-001",
                "source_timezone": "Asia/Shanghai",
                "snapshot_at": "2026-07-31T12:00:00Z",
                "staff_chunk_size": "100",
                "max_attempts": "3",
            },
        )
    ]


def test_result_page_escapes_command_output(tmp_path: Path) -> None:
    executor = FakeExecutor(
        CommandResult(
            action="preflight",
            exit_code=2,
            payload={"status": "<script>alert(1)</script>"},
            error_code="SOURCE_<INVALID>",
        )
    )
    fields = {
        "csrfToken": "test-csrf-token",
        "legacyTenantId": "tenant-001",
        "sourceTimezone": "Asia/Shanghai",
    }
    with running_console(console(tmp_path, executor)) as port:
        status, headers, _ = request(port, "POST", "/actions/preflight", fields)
        assert status == 303
        result_status, _, content = request(port, "GET", headers["location"])

    assert result_status == 200
    assert "<script>alert(1)</script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content
    assert "SOURCE_&lt;INVALID&gt;" in content


def test_cli_executor_keeps_secrets_out_of_process_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"PREFLIGHT_OK","tenantCount":1}\n',
            stderr="",
        )

    monkeypatch.setenv("ETBC_PASSWORD", "source-secret-never-in-argv")
    monkeypatch.setenv("IAM_MIGRATION_INTERNAL_AUTH_TOKEN", "iam-secret-never-in-argv")
    executor = CliExecutor(
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        runner=fake_run,
    )
    result = executor.execute(
        "preflight",
        {"legacy_tenant_id": "tenant-001", "source_timezone": "Asia/Shanghai"},
    )

    command = captured["command"]
    assert isinstance(command, list)
    rendered = " ".join(command)
    assert "source-secret-never-in-argv" not in rendered
    assert "iam-secret-never-in-argv" not in rendered
    assert command[command.index("--enabled-modules") + 1] == "TENANT,ORGANIZATION,STAFF"
    assert "env" not in captured["kwargs"]
    assert result.payload["status"] == "PREFLIGHT_OK"


def test_cli_executor_restarts_frozen_executable_without_python_module_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='{"status":"PREFLIGHT_OK"}\n',
            stderr="",
        )

    executable = r"C:\Program Files\ETBC Migration\etbc-iam-migrate.exe"
    monkeypatch.setattr("etbc_migration.web.sys.executable", executable)
    monkeypatch.setattr("etbc_migration.web.sys.frozen", True, raising=False)
    executor = CliExecutor(
        config_path=tmp_path / "config.toml",
        state_dir=tmp_path / "state",
        runner=fake_run,
    )

    executor.execute(
        "preflight",
        {"legacy_tenant_id": "tenant-001", "source_timezone": "Asia/Shanghai"},
    )

    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == [executable, "--config"]
    assert "-m" not in command
    assert "etbc_migration" not in command


@pytest.mark.parametrize(
    "bind",
    ["127.0.0.1", "localhost", "::1", "0.0.0.0", "192.168.137.213", "::"],
)
def test_web_accepts_configured_listener_address(bind: str) -> None:
    assert validate_web_bind(bind) == bind


def test_web_rejects_empty_listener_address() -> None:
    with pytest.raises(ConfigError, match="WEB_BIND_INVALID"):
        validate_web_bind("  ")


def test_wildcard_listener_accepts_same_origin_lan_form_submission(tmp_path: Path) -> None:
    executor = FakeExecutor()
    with running_console(console(tmp_path, executor), "0.0.0.0") as port:
        authority = f"192.168.137.213:{port}"
        status, headers, _ = request(
            port,
            "POST",
            "/actions/preflight",
            {
                "csrfToken": "test-csrf-token",
                "legacyTenantId": "tenant-001",
                "sourceTimezone": "Asia/Shanghai",
            },
            {"Host": authority, "Origin": f"http://{authority}"},
        )

    assert status == 303
    assert headers["location"].startswith("/results/")
    assert executor.calls == [
        (
            "preflight",
            {"legacy_tenant_id": "tenant-001", "source_timezone": "Asia/Shanghai"},
        )
    ]


def test_csrf_cookie_survives_restart_and_proxy_origin_rewrite(tmp_path: Path) -> None:
    executor = FakeExecutor()
    old_token = "old-csrf-token-1234567890"
    web_console = console(tmp_path, executor, csrf_token=old_token)
    with running_console(web_console, "0.0.0.0") as port:
        get_status, get_headers, _ = request(port, "GET", "/")
        cookie = get_headers["set-cookie"].split(";", 1)[0]
        web_console.csrf_token = "new-csrf-token-0987654321"
        post_status, post_headers, content = request(
            port,
            "POST",
            "/actions/preflight",
            {
                "csrfToken": old_token,
                "legacyTenantId": "tenant-001",
                "sourceTimezone": "Asia/Shanghai",
            },
            {
                "Cookie": cookie,
                "Host": f"migration-backend:{port}",
                "Origin": "https://delivery-console.example.test",
            },
        )

    assert get_status == 200
    assert post_status == 303
    assert post_headers["location"].startswith("/results/")
    assert "请求已失效" not in content
    assert executor.calls == [
        (
            "preflight",
            {"legacy_tenant_id": "tenant-001", "source_timezone": "Asia/Shanghai"},
        )
    ]


def test_cli_web_command_uses_configured_state_and_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        '[migration]\nstate_dir = "/configured/state"\n[web]\ndefault_source_timezone = "Asia/Shanghai"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_serve_web(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("etbc_migration.web.serve_web", fake_serve_web)
    exit_code = cli.main(
        [
            "--config",
            str(config_path),
            "web",
            "--state-dir",
            str(tmp_path / "state"),
            "--bind",
            "127.0.0.1",
            "--port",
            "8090",
        ]
    )

    assert exit_code == 0
    assert captured["config_path"] == str(config_path)
    assert captured["state_dir"] == tmp_path / "state"
    assert captured["bind"] == "127.0.0.1"
    assert captured["port"] == 8090
