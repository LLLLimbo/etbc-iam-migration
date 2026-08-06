from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import load_config, section
from .errors import ConfigError, ExitCode, LocalValidationError, MigrationError
from .iam_client import IamClient, IamClientConfig
from .logging_utils import configure_logging
from .payloads import build_shards
from .reporting import build_report, report_exit_code, write_reports
from .runner import MigrationRunner
from .source import EtbcReader, SourceDatabaseConfig
from .state import StateStore
from .validation import validate_modules, validate_snapshot


class MigrationArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConfigError("CLI_ARGUMENTS_INVALID")


def _parser() -> argparse.ArgumentParser:
    parser = MigrationArgumentParser(prog="etbc-iam-migrate")
    parser.add_argument("--config", help="TOML file containing non-secret connection settings")
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def source_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--legacy-tenant-id", required=True)
        command.add_argument("--source-timezone", required=True)
        command.add_argument("--enabled-modules", required=True)

    preflight = subparsers.add_parser("preflight")
    source_arguments(preflight)

    migrate = subparsers.add_parser("migrate")
    source_arguments(migrate)
    migrate.add_argument("--batch-id", required=True)
    migrate.add_argument("--snapshot-at", required=True)
    migrate.add_argument("--state-dir")
    migrate.add_argument("--iam-url")
    migrate.add_argument("--staff-chunk-size", type=int)
    migrate.add_argument("--max-attempts", type=int)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--batch-id", required=True)
    resume.add_argument("--state-dir")
    resume.add_argument("--iam-url")
    resume.add_argument("--max-attempts", type=int)

    for name in ("report", "verify"):
        report = subparsers.add_parser(name)
        report.add_argument("--batch-id", required=True)
        report.add_argument("--state-dir")
        report.add_argument("--output-dir")

    web = subparsers.add_parser("web")
    web.add_argument("--state-dir")
    web.add_argument(
        "--bind",
        default="127.0.0.1",
        help="listener address; non-loopback addresses expose the unauthenticated console",
    )
    web.add_argument("--port", type=int, default=8080)
    return parser


def _integer(value: Any, default: int, code: str, minimum: int = 1) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ConfigError(code)
    try:
        result = int(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(code) from error
    if result < minimum:
        raise ConfigError(code)
    return result


def _float(value: Any, default: float, code: str, minimum: float = 0.0) -> float:
    if value is None:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ConfigError(code) from error
    if result < minimum:
        raise ConfigError(code)
    return result


def _required_text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(code)
    return value.strip()


def _source_config(config: dict[str, Any]) -> SourceDatabaseConfig:
    values = section(config, "etbc")
    password = os.environ.get("ETBC_PASSWORD")
    if not password:
        raise ConfigError("ETBC_PASSWORD_ENV_REQUIRED")
    return SourceDatabaseConfig(
        host=_required_text(values.get("host"), "ETBC_HOST_REQUIRED"),
        port=_integer(values.get("port"), 3306, "ETBC_PORT_INVALID"),
        user=_required_text(values.get("user"), "ETBC_USER_REQUIRED"),
        schema=_required_text(values.get("schema"), "ETBC_SCHEMA_REQUIRED"),
        password=password,
        connect_timeout_seconds=_integer(
            values.get("connect_timeout_seconds"), 10, "ETBC_CONNECT_TIMEOUT_INVALID"
        ),
        read_timeout_seconds=_integer(
            values.get("read_timeout_seconds"), 60, "ETBC_READ_TIMEOUT_INVALID"
        ),
        ssl_ca=values.get("ssl_ca"),
    )


def _iam_config(config: dict[str, Any], url_override: str | None) -> IamClientConfig:
    values = section(config, "iam")
    base_url = _required_text(url_override or values.get("base_url"), "IAM_BASE_URL_REQUIRED")
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ConfigError("IAM_BASE_URL_INVALID")
    if parsed.scheme != "https" and values.get("allow_insecure_http") is not True:
        raise ConfigError("IAM_HTTPS_REQUIRED")
    return IamClientConfig(
        base_url=base_url,
        connect_timeout_seconds=_float(
            values.get("connect_timeout_seconds"), 5.0, "IAM_CONNECT_TIMEOUT_INVALID", 0.1
        ),
        read_timeout_seconds=_float(
            values.get("read_timeout_seconds"), 60.0, "IAM_READ_TIMEOUT_INVALID", 0.1
        ),
    )


def _migration_settings(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    values = section(config, "migration")
    return {
        "state_dir": Path(args.state_dir or values.get("state_dir", "state")),
        "staff_chunk_size": _integer(
            getattr(args, "staff_chunk_size", None) or values.get("staff_chunk_size"),
            150,
            "STAFF_CHUNK_SIZE_INVALID",
        ),
        "max_attempts": _integer(
            getattr(args, "max_attempts", None) or values.get("max_attempts"),
            3,
            "MAX_ATTEMPTS_INVALID",
        ),
        "retry_base_seconds": _float(
            values.get("retry_base_seconds"), 1.0, "RETRY_BASE_SECONDS_INVALID"
        ),
    }


def _modules(raw: str) -> list[str]:
    modules = [value.strip() for value in raw.split(",")]
    return list(validate_modules(modules))


def _snapshot_at(raw: str) -> str:
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise LocalValidationError("SNAPSHOT_AT_INVALID") from error
    if value.tzinfo is None:
        raise LocalValidationError("SNAPSHOT_AT_INVALID")
    return value.isoformat().replace("+00:00", "Z")


def _metadata(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "migrationBatchId": args.batch_id,
        "legacyTenantId": args.legacy_tenant_id,
        "enabledModules": _modules(args.enabled_modules),
        "sourceTimezone": args.source_timezone,
        "snapshotAt": _snapshot_at(args.snapshot_at),
    }


def _print_json(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _preflight(config: dict[str, Any], args: argparse.Namespace) -> int:
    source = EtbcReader(_source_config(config)).read_snapshot(
        args.legacy_tenant_id, args.source_timezone
    )
    metadata = {
        "migrationBatchId": "preflight",
        "legacyTenantId": args.legacy_tenant_id,
        "enabledModules": _modules(args.enabled_modules),
        "sourceTimezone": args.source_timezone,
        "snapshotAt": source["captured_at"],
    }
    validate_snapshot(source, metadata)
    _print_json(
        {
            "status": "PREFLIGHT_OK",
            "legacyTenantId": args.legacy_tenant_id,
            "capturedAt": source["captured_at"],
            "tenantCount": 1,
            "organizationCount": len(source["organizations"]),
            "staffCount": len(source["staff"]),
            "enabledModules": metadata["enabledModules"],
        }
    )
    return int(ExitCode.SUCCESS)


def _run_and_report(
    store: StateStore,
    batch_id: str,
    client_config: IamClientConfig,
    settings: dict[str, Any],
    *,
    replay_completed: bool = False,
) -> int:
    error: MigrationError | None = None
    try:
        MigrationRunner(
            store,
            IamClient(client_config),
            base_delay_seconds=settings["retry_base_seconds"],
        ).run(
            batch_id,
            max_attempts=settings["max_attempts"],
            replay_completed=replay_completed,
        )
    except MigrationError as caught:
        error = caught
    paths = write_reports(store, batch_id, settings["state_dir"] / "reports")
    _print_json(
        {
            "batchId": batch_id,
            "jsonReport": str(paths[0]),
            "markdownReport": str(paths[1]),
            "status": build_report(store, batch_id)["overallStatus"],
        }
    )
    if error is not None:
        raise error
    return int(ExitCode.SUCCESS)


def _migrate(config: dict[str, Any], args: argparse.Namespace) -> int:
    settings = _migration_settings(config, args)
    metadata = _metadata(args)
    client_config = _iam_config(config, args.iam_url)
    with StateStore(settings["state_dir"]) as store:
        existing_snapshot = store.has_batch(args.batch_id)
        if existing_snapshot:
            if store.load_metadata(args.batch_id) != metadata:
                raise LocalValidationError("MIGRATION_BATCH_METADATA_MISMATCH")
        else:
            source_config = _source_config(config)
            snapshot = EtbcReader(source_config).read_snapshot(
                args.legacy_tenant_id, args.source_timezone
            )
            validate_snapshot(snapshot, metadata)
            shards = build_shards(snapshot, metadata, settings["staff_chunk_size"])
            store.create_snapshot(
                metadata,
                shards,
                {
                    "capturedAt": snapshot["captured_at"],
                    "sourceHost": source_config.host,
                    "sourceSchema": source_config.schema,
                    "tenantCount": 1,
                    "organizationCount": len(snapshot["organizations"]),
                    "staffCount": len(snapshot["staff"]),
                    "shardCount": len(shards),
                },
            )
        return _run_and_report(
            store,
            args.batch_id,
            client_config,
            settings,
            replay_completed=existing_snapshot,
        )


def _resume(config: dict[str, Any], args: argparse.Namespace) -> int:
    settings = _migration_settings(config, args)
    client_config = _iam_config(config, args.iam_url)
    with StateStore(settings["state_dir"]) as store:
        store.load_metadata(args.batch_id)
        return _run_and_report(store, args.batch_id, client_config, settings)


def _report(config: dict[str, Any], args: argparse.Namespace) -> int:
    settings = _migration_settings(config, args)
    output_dir = Path(args.output_dir) if args.output_dir else settings["state_dir"] / "reports"
    with StateStore(settings["state_dir"]) as store:
        paths = write_reports(store, args.batch_id, output_dir)
        report = build_report(store, args.batch_id)
    _print_json(
        {
            "batchId": args.batch_id,
            "jsonReport": str(paths[0]),
            "markdownReport": str(paths[1]),
            "status": report["overallStatus"],
        }
    )
    return int(report_exit_code(report))


def _web(config: dict[str, Any], args: argparse.Namespace) -> int:
    if args.config is None:
        raise ConfigError("WEB_CONFIG_REQUIRED")
    settings = _migration_settings(config, args)
    from .web import serve_web

    return serve_web(
        config_path=args.config,
        state_dir=settings["state_dir"],
        config=config,
        bind=args.bind,
        port=args.port,
    )


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = load_config(args.config)
        secrets = [os.environ.get("ETBC_PASSWORD", "")]
        configure_logging(args.verbose, secrets)
        if args.command == "preflight":
            return _preflight(config, args)
        if args.command == "migrate":
            return _migrate(config, args)
        if args.command == "resume":
            return _resume(config, args)
        if args.command in {"report", "verify"}:
            return _report(config, args)
        if args.command == "web":
            return _web(config, args)
        raise ConfigError("CLI_COMMAND_INVALID")
    except MigrationError as error:
        print(f"ERROR {error.code}", file=sys.stderr)
        return int(error.exit_code)
