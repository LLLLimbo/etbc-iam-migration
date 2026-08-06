from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from html import escape
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .errors import ConfigError, ExitCode, MigrationError
from .state import StateStore


LOGGER = logging.getLogger(__name__)
CORE_MODULES = "TENANT,ORGANIZATION,STAFF"
_MAX_FORM_BYTES = 16 * 1024
_MAX_RESULTS = 32
_ERROR_CODE = re.compile(r"\bERROR ([A-Z][A-Z0-9_]*)\b")
_RESULT_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")
_CSRF_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")
_CSRF_COOKIE_NAME = "etbc_migration_csrf"
_SAFE_PAYLOAD_KEYS = {
    "status",
    "batchId",
    "legacyTenantId",
    "capturedAt",
    "tenantCount",
    "organizationCount",
    "staffCount",
    "enabledModules",
    "jsonReport",
    "markdownReport",
}


@dataclass(frozen=True)
class CommandResult:
    action: str
    exit_code: int
    payload: dict[str, Any]
    error_code: str | None


class WebInputError(Exception):
    pass


class CliExecutor:
    """Run the installed CLI so web and shell operations share one implementation."""

    def __init__(
        self,
        config_path: str | Path,
        state_dir: str | Path,
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._config_path = Path(config_path).resolve()
        self._state_dir = Path(state_dir).resolve()
        self._runner = runner

    @staticmethod
    def _option(name: str, value: str) -> str:
        return f"--{name}={value}"

    def _command(self, action: str, values: dict[str, str]) -> list[str]:
        command = [sys.executable]
        if not getattr(sys, "frozen", False):
            command.extend(["-m", "etbc_migration"])
        command.extend(
            [
                "--config",
                str(self._config_path),
                action,
            ]
        )
        if action == "preflight":
            command.extend(
                [
                    self._option("legacy-tenant-id", values["legacy_tenant_id"]),
                    self._option("source-timezone", values["source_timezone"]),
                    "--enabled-modules",
                    CORE_MODULES,
                ]
            )
        elif action == "migrate":
            command.extend(
                [
                    self._option("batch-id", values["batch_id"]),
                    self._option("legacy-tenant-id", values["legacy_tenant_id"]),
                    self._option("source-timezone", values["source_timezone"]),
                    "--enabled-modules",
                    CORE_MODULES,
                    self._option("snapshot-at", values["snapshot_at"]),
                    self._option("state-dir", str(self._state_dir)),
                ]
            )
            if values.get("staff_chunk_size"):
                command.append(self._option("staff-chunk-size", values["staff_chunk_size"]))
            if values.get("max_attempts"):
                command.append(self._option("max-attempts", values["max_attempts"]))
        elif action == "resume":
            command.extend(
                [
                    self._option("batch-id", values["batch_id"]),
                    self._option("state-dir", str(self._state_dir)),
                ]
            )
            if values.get("max_attempts"):
                command.append(self._option("max-attempts", values["max_attempts"]))
        elif action == "report":
            command.extend(
                [
                    self._option("batch-id", values["batch_id"]),
                    self._option("state-dir", str(self._state_dir)),
                    self._option("output-dir", str(self._state_dir / "reports")),
                ]
            )
        else:
            raise ConfigError("WEB_ACTION_INVALID")
        return command

    def execute(self, action: str, values: dict[str, str]) -> CommandResult:
        command = self._command(action, values)
        completed = self._runner(command, capture_output=True, text=True, check=False)
        raw_output = completed.stdout.strip()
        payload: dict[str, Any] = {}
        output_invalid = False
        if raw_output and len(raw_output) <= 64 * 1024:
            try:
                parsed = json.loads(raw_output.splitlines()[-1])
                if isinstance(parsed, dict):
                    payload = {
                        key: (Path(value).name if key in {"jsonReport", "markdownReport"} else value)
                        for key, value in parsed.items()
                        if key in _SAFE_PAYLOAD_KEYS
                    }
                else:
                    output_invalid = True
            except (json.JSONDecodeError, TypeError, ValueError):
                output_invalid = True
        elif completed.returncode == 0:
            output_invalid = True

        match = _ERROR_CODE.search(completed.stderr[-4096:])
        error_code = match.group(1) if match else None
        exit_code = completed.returncode
        if output_invalid:
            exit_code = int(ExitCode.NETWORK_PROTOCOL)
            error_code = "WEB_CLI_OUTPUT_INVALID"
            payload = {}
        elif exit_code != 0 and error_code is None:
            error_code = "WEB_COMMAND_FAILED"
        return CommandResult(action, exit_code, payload, error_code)


class _ResultStore:
    def __init__(self) -> None:
        self._items: OrderedDict[str, CommandResult] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, result: CommandResult) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._items[token] = result
            while len(self._items) > _MAX_RESULTS:
                self._items.popitem(last=False)
        return token

    def get(self, token: str) -> CommandResult | None:
        with self._lock:
            return self._items.get(token)


class WebConsole:
    def __init__(
        self,
        *,
        config_path: str | Path,
        state_dir: str | Path,
        config: dict[str, Any],
        executor: CliExecutor | Any | None = None,
        csrf_token: str | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.state_dir = Path(state_dir).resolve()
        migration = config.get("migration", {})
        iam = config.get("iam", {})
        web = config.get("web", {})
        self.default_staff_chunk_size = str(migration.get("staff_chunk_size", 150))
        self.default_max_attempts = str(migration.get("max_attempts", 3))
        self.default_source_timezone = str(web.get("default_source_timezone", "Asia/Shanghai"))
        self.iam_endpoint_configured = bool(iam.get("base_url"))
        self.executor = executor or CliExecutor(self.config_path, self.state_dir)
        self.csrf_token = csrf_token or secrets.token_urlsafe(32)
        self.results = _ResultStore()
        self.operation_lock = threading.Lock()

    def list_batch_overviews(self) -> tuple[list[dict[str, Any]], str | None]:
        try:
            with StateStore(self.state_dir) as store:
                batches = store.list_batches()
                for batch in batches:
                    entities = store.list_entities(batch["batchId"])
                    statuses = Counter(item["finalStatus"] for item in entities)
                    batch["entityCount"] = len(entities)
                    batch["summary"] = dict(sorted(statuses.items()))
                    batch["overallStatus"] = (
                        "SUCCESS"
                        if entities
                        and all(
                            item["finalStatus"] in {"SUCCESS", "ALREADY_EXISTS"}
                            for item in entities
                        )
                        else "INCOMPLETE_OR_FAILED"
                    )
                return batches, None
        except MigrationError as error:
            return [], error.code

    def batch_overview(self, batch_id: str) -> dict[str, Any]:
        with StateStore(self.state_dir) as store:
            metadata = store.load_metadata(batch_id)
            entities = store.list_entities(batch_id)
            calls = store.list_calls(batch_id)
        summary = Counter(item["finalStatus"] for item in entities)
        by_type: dict[str, Counter[str]] = {}
        for item in entities:
            by_type.setdefault(item["entityType"], Counter())[item["finalStatus"]] += 1
        return {
            "metadata": metadata,
            "summary": dict(sorted(summary.items())),
            "entitySummary": {
                entity_type: dict(sorted(counts.items()))
                for entity_type, counts in sorted(by_type.items())
            },
            "calls": calls,
            "overallStatus": (
                "SUCCESS"
                if entities
                and all(item["finalStatus"] in {"SUCCESS", "ALREADY_EXISTS"} for item in entities)
                else "INCOMPLETE_OR_FAILED"
            ),
        }


def validate_web_bind(bind: str) -> str:
    candidate = bind.strip()
    if (
        not candidate
        or len(candidate) > 255
        or any(character.isspace() or ord(character) < 32 for character in candidate)
    ):
        raise ConfigError("WEB_BIND_INVALID")
    return candidate


def _e(value: object) -> str:
    return escape(str(value), quote=True)


def _status_tone(status: str) -> str:
    if status in {"SUCCESS", "ALREADY_EXISTS", "PREFLIGHT_OK"}:
        return "success"
    if status in {"PENDING", "PROCESS_FAILED", "INCOMPLETE_OR_FAILED"}:
        return "warning"
    return "danger"


def _layout(title: str, body: str, *, page: str = "console") -> str:
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light">
  <title>{_e(title)} · ETBC → IAM</title>
  <link rel="stylesheet" href="/assets/app.css">
  <script src="/assets/app.js" defer></script>
</head>
<body data-page="{_e(page)}">
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <header class="masthead">
    <a class="wordmark" href="/" aria-label="返回迁移控制台首页">
      <span class="wordmark-mark" aria-hidden="true">EI</span>
      <span><b>ETBC / IAM</b><small>Delivery migration desk</small></span>
    </a>
    <div class="module-lock"><span aria-hidden="true"></span>TENANT · ORGANIZATION · STAFF</div>
  </header>
  <main id="main-content">{body}</main>
  <footer class="footer">
    <span>只读源快照 · IAM API 写入 · SQLite 可恢复台账</span>
    <span>仅限受控交付环境</span>
  </footer>
</body>
</html>"""


def _credential_state(name: str, ready: bool) -> str:
    label = "已注入" if ready else "未注入"
    tone = "ready" if ready else "missing"
    return (
        f'<li><span class="signal {tone}" aria-hidden="true"></span>'
        f'<span>{_e(name)}</span><strong>{label}</strong></li>'
    )


def _csrf(console: WebConsole) -> str:
    return f'<input type="hidden" name="csrfToken" value="{_e(console.csrf_token)}">'


def _home(console: WebConsole) -> str:
    batches, state_error = console.list_batch_overviews()
    batch_rows: list[str] = []
    for batch in batches:
        status = batch["overallStatus"]
        label = "完成" if status == "SUCCESS" else "需处理"
        summary = " · ".join(f"{key} {value}" for key, value in batch["summary"].items()) or "尚未执行"
        batch_rows.append(
            "<tr>"
            f'<td data-label="批次"><a class="batch-link" href="/batches?batchId={quote(str(batch["batchId"]))}">{_e(batch["batchId"])}</a>'
            f'<small>租户 {_e(batch["legacyTenantId"])}</small></td>'
            f'<td data-label="状态"><span class="status { _status_tone(status) }">{label}</span><small>{_e(summary)}</small></td>'
            f'<td data-label="实体">{_e(batch["entityCount"])}</td>'
            f'<td data-label="更新时间"><time>{_e(batch["updatedAt"])}</time></td>'
            "</tr>"
        )
    if not batch_rows:
        message = (
            f"状态目录暂不可读取：{_e(state_error)}。请检查权限后刷新。"
            if state_error
            else "还没有本地批次。先完成 preflight，再创建第一份固定迁移快照。"
        )
        batch_rows.append(f'<tr><td colspan="4"><div class="empty-state">{message}</div></td></tr>')

    body = f"""
<section class="intro" aria-labelledby="page-title">
  <div class="intro-copy">
    <p class="eyebrow">DELIVERY RUNBOOK / 01—04</p>
    <h1 id="page-title" aria-label="ETBC → IAM 迁移控制台">ETBC → IAM<br><em>迁移控制台</em></h1>
    <p class="lede">为交付工程师提供一条可验证、可恢复、可审计的租户迁移路径。页面仅展示运行证据，不展示源数据明细。</p>
  </div>
  <aside class="readiness" aria-labelledby="readiness-title">
    <div class="readiness-heading"><span>运行准备</span><b id="readiness-title">环境信号</b></div>
    <ul>
      {_credential_state("ETBC 只读凭据", bool(os.environ.get("ETBC_PASSWORD")))}
      {_credential_state("IAM 接口地址", console.iam_endpoint_configured)}
      {_credential_state("本地状态目录", True)}
    </ul>
    <p>ETBC 密码不会进入页面。控制台不提供登录认证；非回环监听仅用于受控交付网络。</p>
  </aside>
</section>

<div class="workbench">
  <section class="runbook" id="runbook" aria-labelledby="runbook-title">
    <div class="section-heading">
      <p class="eyebrow">CONTROL PATH</p>
      <h2 id="runbook-title">执行路径</h2>
    </div>

    <details class="step" open>
      <summary><span class="step-no">01</span><span><b>源数据检查</b><small>只读，不调用 IAM</small></span></summary>
      <div class="step-body">
        <form method="post" action="/actions/preflight" data-operation-form>
          {_csrf(console)}
          <div class="field-grid">
            <label><span>Legacy tenant ID</span><input name="legacyTenantId" required maxlength="256" autocomplete="off" spellcheck="false"></label>
            <label><span>源时区</span><input name="sourceTimezone" required maxlength="64" value="{_e(console.default_source_timezone)}" autocomplete="off" spellcheck="false"></label>
          </div>
          <div class="fixed-modules"><span>固定模块</span><output>TENANT · ORGANIZATION · STAFF</output></div>
          <button class="button primary" type="submit" data-busy-label="正在检查源数据…">运行 preflight</button>
        </form>
      </div>
    </details>

    <details class="step">
      <summary><span class="step-no">02</span><span><b>创建快照并迁移</b><small>建立不可变批次并写入 IAM</small></span></summary>
      <div class="step-body">
        <form method="post" action="/actions/migrate" data-operation-form>
          {_csrf(console)}
          <div class="field-grid">
            <label><span>Migration batch ID</span><span class="input-action"><input name="batchId" required maxlength="256" autocomplete="off" spellcheck="false"><button type="button" data-generate-batch>生成</button></span></label>
            <label><span>Legacy tenant ID</span><input name="legacyTenantId" required maxlength="256" autocomplete="off" spellcheck="false"></label>
            <label><span>UTC snapshotAt</span><input name="snapshotAt" required maxlength="64" placeholder="2026-07-31T12:00:00Z" autocomplete="off" spellcheck="false"></label>
            <label><span>源时区</span><input name="sourceTimezone" required maxlength="64" value="{_e(console.default_source_timezone)}" autocomplete="off" spellcheck="false"></label>
          </div>
          <details class="advanced">
            <summary>分片与重试设置</summary>
            <div class="field-grid compact">
              <label><span>员工分片</span><input name="staffChunkSize" type="number" min="1" max="1000" value="{_e(console.default_staff_chunk_size)}" required></label>
              <label><span>最大尝试次数</span><input name="maxAttempts" type="number" min="1" max="10" value="{_e(console.default_max_attempts)}" required></label>
            </div>
          </details>
          <label class="write-confirm"><input type="checkbox" name="confirmWrite" value="confirmed" required><span><b>确认写入 IAM</b><small>该操作会先固化本地快照，再调用 IAM 内部批量导入接口。</small></span></label>
          <button class="button danger" type="submit" data-busy-label="正在创建快照并迁移…">创建批次并迁移</button>
        </form>
      </div>
    </details>

    <details class="step">
      <summary><span class="step-no">03</span><span><b>继续未完成项</b><small>复用固定负载，不重新读取 ETBC</small></span></summary>
      <div class="step-body">
        <form method="post" action="/actions/resume" data-operation-form>
          {_csrf(console)}
          <div class="field-grid compact">
            <label><span>Migration batch ID</span><input name="batchId" required maxlength="256" autocomplete="off" spellcheck="false"></label>
            <label><span>最大尝试次数</span><input name="maxAttempts" type="number" min="1" max="10" value="{_e(console.default_max_attempts)}" required></label>
          </div>
          <label class="write-confirm"><input type="checkbox" name="confirmWrite" value="confirmed" required><span><b>确认继续 IAM 写入</b><small>只处理本地台账中的非终态实体。</small></span></label>
          <button class="button secondary" type="submit" data-busy-label="正在恢复迁移…">Resume 批次</button>
        </form>
      </div>
    </details>

    <details class="step">
      <summary><span class="step-no">04</span><span><b>生成验收报告</b><small>输出受限权限的 JSON 与 Markdown</small></span></summary>
      <div class="step-body">
        <form method="post" action="/actions/report" data-operation-form>
          {_csrf(console)}
          <label><span>Migration batch ID</span><input name="batchId" required maxlength="256" autocomplete="off" spellcheck="false"></label>
          <button class="button secondary" type="submit" data-busy-label="正在生成报告…">生成验收报告</button>
        </form>
      </div>
    </details>
  </section>

  <section class="ledger" id="ledger" aria-labelledby="ledger-title">
    <div class="section-heading ruled">
      <div><p class="eyebrow">LOCAL LEDGER</p><h2 id="ledger-title">批次台账</h2></div>
      <span>{len(batches):02d} BATCHES</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>批次</th><th>状态</th><th>实体</th><th>更新时间</th></tr></thead>
        <tbody>{''.join(batch_rows)}</tbody>
      </table>
    </div>
  </section>
</div>
"""
    return _layout("迁移控制台", body)


def _result_page(result: CommandResult) -> str:
    success = result.exit_code == 0
    title = "操作完成" if success else "操作未完成"
    status = str(result.payload.get("status") or ("SUCCESS" if success else "FAILED"))
    details: list[tuple[str, object]] = [("退出码", result.exit_code), ("状态", status)]
    labels = {
        "batchId": "批次 ID",
        "tenantCount": "租户",
        "organizationCount": "组织",
        "staffCount": "员工",
        "capturedAt": "源快照时间",
        "jsonReport": "JSON 报告",
        "markdownReport": "Markdown 报告",
    }
    for key, label in labels.items():
        if key in result.payload:
            details.append((label, result.payload[key]))
    if result.error_code:
        details.append(("错误码", result.error_code))
    rows = "".join(f"<div><dt>{_e(label)}</dt><dd>{_e(value)}</dd></div>" for label, value in details)
    batch_id = result.payload.get("batchId")
    batch_link = (
        f'<a class="button secondary" href="/batches?batchId={quote(str(batch_id))}">查看批次汇总</a>'
        if batch_id
        else ""
    )
    body = f"""
<section class="result-shell">
  <p class="eyebrow">OPERATION RESULT / {_e(result.action.upper())}</p>
  <div class="result-mark {_status_tone(status)}" aria-hidden="true">{'✓' if success else '!'}</div>
  <h1>{title}</h1>
  <p>{'台账已更新，可继续下一步。' if success else '没有隐藏错误；请根据错误码修正后再执行。'}</p>
  <dl class="evidence">{rows}</dl>
  <div class="result-actions">{batch_link}<a class="button ghost" href="/">返回迁移控制台</a></div>
</section>"""
    return _layout(title, body, page="result")


def _batch_page(console: WebConsole, batch_id: str) -> str:
    overview = console.batch_overview(batch_id)
    metadata = overview["metadata"]
    summary_rows = "".join(
        f'<tr><td data-label="状态"><span class="status {_status_tone(status)}">{_e(status)}</span></td><td data-label="数量">{_e(count)}</td></tr>'
        for status, count in overview["summary"].items()
    ) or '<tr><td colspan="2"><div class="empty-state">尚无实体结果。</div></td></tr>'
    type_rows: list[str] = []
    for entity_type, statuses in overview["entitySummary"].items():
        type_rows.append(
            f'<tr><td data-label="实体类型">{_e(entity_type)}</td><td data-label="结果">'
            + " · ".join(f"{_e(key)} {_e(value)}" for key, value in statuses.items())
            + "</td></tr>"
        )
    call_rows = "".join(
        "<tr>"
        f'<td data-label="分片">{_e(call["shard_index"])}</td>'
        f'<td data-label="结果">{_e(call["outcome"])}</td>'
        f'<td data-label="耗时">{_e(call["duration_ms"])} ms</td>'
        f'<td data-label="错误码">{_e(call["error_code"] or "—")}</td>'
        "</tr>"
        for call in overview["calls"][-20:]
    ) or '<tr><td colspan="4"><div class="empty-state">尚无 IAM 调用记录。</div></td></tr>'
    body = f"""
<section class="batch-hero">
  <a class="back-link" href="/">← 返回迁移控制台</a>
  <p class="eyebrow">IMMUTABLE BATCH</p>
  <h1>{_e(metadata['migrationBatchId'])}</h1>
  <span class="status {_status_tone(overview['overallStatus'])}">{_e(overview['overallStatus'])}</span>
  <dl class="batch-metadata">
    <div><dt>Legacy tenant ID</dt><dd>{_e(metadata['legacyTenantId'])}</dd></div>
    <div><dt>Snapshot at</dt><dd>{_e(metadata['snapshotAt'])}</dd></div>
    <div><dt>源时区</dt><dd>{_e(metadata['sourceTimezone'])}</dd></div>
    <div><dt>固定模块</dt><dd>{_e(' · '.join(metadata['enabledModules']))}</dd></div>
  </dl>
</section>
<section class="batch-grid">
  <div><div class="section-heading ruled"><h2>结果分布</h2></div><table><thead><tr><th>状态</th><th>数量</th></tr></thead><tbody>{summary_rows}</tbody></table></div>
  <div><div class="section-heading ruled"><h2>实体汇总</h2></div><table><thead><tr><th>实体类型</th><th>结果</th></tr></thead><tbody>{''.join(type_rows)}</tbody></table></div>
</section>
<section class="calls">
  <div class="section-heading ruled"><h2>最近 IAM 调用</h2><span>最多显示 20 条</span></div>
  <div class="table-wrap"><table><thead><tr><th>分片</th><th>结果</th><th>耗时</th><th>错误码</th></tr></thead><tbody>{call_rows}</tbody></table></div>
</section>
<section class="batch-actions" aria-label="批次操作">
  <form method="post" action="/actions/report" data-operation-form>{_csrf(console)}<input type="hidden" name="batchId" value="{_e(metadata['migrationBatchId'])}"><button class="button secondary" type="submit" data-busy-label="正在生成报告…">生成验收报告</button></form>
  <form method="post" action="/actions/resume" data-operation-form>{_csrf(console)}<input type="hidden" name="batchId" value="{_e(metadata['migrationBatchId'])}"><input type="hidden" name="maxAttempts" value="{_e(console.default_max_attempts)}"><label class="write-confirm compact-confirm"><input type="checkbox" name="confirmWrite" value="confirmed" required><span><b>确认继续 IAM 写入</b></span></label><button class="button danger" type="submit" data-busy-label="正在恢复迁移…">Resume 未完成项</button></form>
</section>"""
    return _layout(f"批次 {metadata['migrationBatchId']}", body, page="batch")


def _error_page(title: str, message: str) -> str:
    body = f"""
<section class="result-shell">
  <p class="eyebrow">REQUEST STOPPED</p>
  <div class="result-mark danger" aria-hidden="true">!</div>
  <h1>{_e(title)}</h1>
  <p>{_e(message)}</p>
  <div class="result-actions"><a class="button ghost" href="/">返回迁移控制台</a></div>
</section>"""
    return _layout(title, body, page="result")


def _field(form: dict[str, str], name: str, label: str, *, maximum: int = 256) -> str:
    value = form.get(name, "").strip()
    if not value:
        raise WebInputError(f"请填写{label}。")
    if len(value) > maximum or any(ord(character) < 32 for character in value):
        raise WebInputError(f"{label}格式不正确，请检查后重试。")
    return value


def _bounded_integer(form: dict[str, str], name: str, label: str, minimum: int, maximum: int) -> str:
    raw = _field(form, name, label, maximum=8)
    try:
        value = int(raw)
    except ValueError as error:
        raise WebInputError(f"{label}需要是整数。") from error
    if value < minimum or value > maximum:
        raise WebInputError(f"{label}需要在 {minimum} 到 {maximum} 之间。")
    return str(value)


def _operation_values(action: str, form: dict[str, str]) -> dict[str, str]:
    if action == "preflight":
        return {
            "legacy_tenant_id": _field(form, "legacyTenantId", " Legacy tenant ID"),
            "source_timezone": _field(form, "sourceTimezone", "源时区", maximum=64),
        }
    if action == "migrate":
        if form.get("confirmWrite") != "confirmed":
            raise WebInputError("请确认写入 IAM 后再开始正式迁移。")
        return {
            "legacy_tenant_id": _field(form, "legacyTenantId", " Legacy tenant ID"),
            "batch_id": _field(form, "batchId", " Migration batch ID"),
            "source_timezone": _field(form, "sourceTimezone", "源时区", maximum=64),
            "snapshot_at": _field(form, "snapshotAt", " UTC snapshotAt", maximum=64),
            "staff_chunk_size": _bounded_integer(form, "staffChunkSize", "员工分片", 1, 1000),
            "max_attempts": _bounded_integer(form, "maxAttempts", "最大尝试次数", 1, 10),
        }
    if action == "resume":
        if form.get("confirmWrite") != "confirmed":
            raise WebInputError("请确认继续 IAM 写入后再恢复批次。")
        return {
            "batch_id": _field(form, "batchId", " Migration batch ID"),
            "max_attempts": _bounded_integer(form, "maxAttempts", "最大尝试次数", 1, 10),
        }
    if action == "report":
        return {"batch_id": _field(form, "batchId", " Migration batch ID")}
    raise WebInputError("该操作不存在，请返回控制台重新选择。")


def _asset(name: str) -> bytes:
    return files("etbc_migration.web_assets").joinpath(name).read_bytes()


class _ConsoleServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], console: WebConsole) -> None:
        self.console = console
        super().__init__(address, _Handler)


class _ConsoleServerV6(_ConsoleServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server: _ConsoleServer
    server_version = "EtbcIamMigrationWeb/1.0"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        LOGGER.info(
            "Web request: client=%s method=%s path=%s",
            self.client_address[0],
            self.command,
            urlsplit(self.path).path,
        )

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        *,
        set_csrf_cookie: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; base-uri 'none'; connect-src 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        if set_csrf_cookie:
            self.send_header(
                "Set-Cookie",
                f"{_CSRF_COOKIE_NAME}={self.server.console.csrf_token}; "
                "Path=/; HttpOnly; SameSite=Strict",
            )
        self.end_headers()

    def _send(
        self,
        status: int,
        body: str | bytes,
        content_type: str = "text/html; charset=utf-8",
        *,
        set_csrf_cookie: bool = False,
    ) -> None:
        encoded = body.encode("utf-8") if isinstance(body, str) else body
        self._headers(status, content_type, len(encoded), set_csrf_cookie=set_csrf_cookie)
        self.wfile.write(encoded)

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._send(
                HTTPStatus.OK,
                _home(self.server.console),
                set_csrf_cookie=True,
            )
            return
        if parsed.path == "/healthz":
            self._send(
                HTTPStatus.OK,
                b'{"status":"ok"}',
                "application/json; charset=utf-8",
            )
            return
        if parsed.path == "/assets/app.css":
            self._send(HTTPStatus.OK, _asset("app.css"), "text/css; charset=utf-8")
            return
        if parsed.path == "/assets/app.js":
            self._send(HTTPStatus.OK, _asset("app.js"), "text/javascript; charset=utf-8")
            return
        if parsed.path.startswith("/results/"):
            token = parsed.path.removeprefix("/results/")
            result = (
                self.server.console.results.get(token)
                if _RESULT_TOKEN.fullmatch(token)
                else None
            )
            if result is None:
                self._send(
                    HTTPStatus.NOT_FOUND,
                    _error_page("结果已失效", "该临时结果不存在。批次台账仍保存在本地状态目录中。"),
                )
            else:
                self._send(HTTPStatus.OK, _result_page(result))
            return
        if parsed.path == "/batches":
            query = parse_qs(parsed.query, keep_blank_values=True, max_num_fields=4)
            batch_id = query.get("batchId", [""])[0]
            try:
                if not batch_id or len(batch_id) > 256:
                    raise WebInputError("请提供有效的批次 ID。")
                self._send(
                    HTTPStatus.OK,
                    _batch_page(self.server.console, batch_id),
                    set_csrf_cookie=True,
                )
            except (MigrationError, WebInputError) as error:
                message = error.code if isinstance(error, MigrationError) else str(error)
                self._send(HTTPStatus.NOT_FOUND, _error_page("找不到批次", message))
            return
        self._send(HTTPStatus.NOT_FOUND, _error_page("页面不存在", "请返回迁移控制台重新选择操作。"))

    def _valid_csrf(self, submitted: str) -> bool:
        if submitted and hmac.compare_digest(submitted, self.server.console.csrf_token):
            return True
        if not _CSRF_TOKEN.fullmatch(submitted):
            return False
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return False
        morsel = cookies.get(_CSRF_COOKIE_NAME)
        return morsel is not None and hmac.compare_digest(submitted, morsel.value)

    def _read_form(self) -> dict[str, str]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise WebInputError("请求格式不正确，请刷新页面后重试。")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise WebInputError("请求长度不正确，请刷新页面后重试。") from error
        if length <= 0 or length > _MAX_FORM_BYTES:
            raise WebInputError("请求内容大小不正确，请刷新页面后重试。")
        try:
            raw = self.rfile.read(length).decode("utf-8")
            parsed = parse_qs(raw, keep_blank_values=True, max_num_fields=32, strict_parsing=True)
        except (UnicodeDecodeError, ValueError) as error:
            raise WebInputError("请求内容无法解析，请刷新页面后重试。") from error
        if any(len(values) != 1 for values in parsed.values()):
            raise WebInputError("请求中存在重复字段，请刷新页面后重试。")
        return {key: values[0] for key, values in parsed.items()}

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/actions/"):
            self._send(HTTPStatus.NOT_FOUND, _error_page("操作不存在", "请返回迁移控制台重新选择操作。"))
            return
        action = parsed.path.removeprefix("/actions/")
        try:
            form = self._read_form()
            if not self._valid_csrf(form.get("csrfToken", "")):
                LOGGER.warning(
                    "Web request rejected: client=%s reason=csrf_mismatch",
                    self.client_address[0],
                )
                self._send(
                    HTTPStatus.FORBIDDEN,
                    _error_page(
                        "页面校验信息已更新",
                        "请刷新迁移控制台，再重新执行该操作。",
                    ),
                )
                return
            values = _operation_values(action, form)
        except WebInputError as error:
            self._send(HTTPStatus.BAD_REQUEST, _error_page("无法开始操作", str(error)))
            return

        if not self.server.console.operation_lock.acquire(blocking=False):
            self._send(
                HTTPStatus.CONFLICT,
                _error_page("已有操作正在执行", "请等待当前操作完成，再开始下一项迁移任务。"),
            )
            return
        try:
            result = self.server.console.executor.execute(action, values)
        except Exception:
            LOGGER.exception("Web command execution failed without exposing command output")
            result = CommandResult(action, int(ExitCode.CONFIG), {}, "WEB_COMMAND_EXECUTION_FAILED")
        finally:
            self.server.console.operation_lock.release()
        token = self.server.console.results.put(result)
        self._redirect(f"/results/{token}")


def create_server(console: WebConsole, bind: str, port: int) -> ThreadingHTTPServer:
    validated = validate_web_bind(bind)
    server_type = _ConsoleServerV6 if ":" in validated else _ConsoleServer
    try:
        return server_type((validated, port), console)
    except OSError as error:
        raise ConfigError("WEB_LISTEN_FAILED") from error


def serve_web(
    *,
    config_path: str | Path,
    state_dir: str | Path,
    config: dict[str, Any],
    bind: str = "127.0.0.1",
    port: int = 8080,
) -> int:
    if port < 1 or port > 65535:
        raise ConfigError("WEB_PORT_INVALID")
    console = WebConsole(config_path=config_path, state_dir=state_dir, config=config)
    server = create_server(console, bind, port)
    LOGGER.info("Migration web console listening on http://%s:%d", bind, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Migration web console stopped")
    finally:
        server.server_close()
    return int(ExitCode.SUCCESS)
