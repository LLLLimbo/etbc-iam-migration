from __future__ import annotations

from pathlib import Path


def test_mysql_fixture_is_readable_by_container_init_user() -> None:
    fixture = Path("integration/etbc-init/00-schema.sql")
    assert fixture.stat().st_mode & 0o444 == 0o444


def test_compose_has_no_successful_one_shot_image_service_that_aborts_test_run() -> None:
    compose = Path("docker-compose.integration.yml").read_text(encoding="utf-8")
    assert "\n  migration-image:" not in compose
    assert compose.count("build:\n      context: .\n      dockerfile: Dockerfile") == 2


def test_compose_has_no_successfully_completed_dependency_that_aborts_test_run() -> None:
    compose = Path("docker-compose.integration.yml").read_text(encoding="utf-8")
    assert "condition: service_completed_successfully" not in compose
    assert "etbc-readonly-init:\n        condition: service_healthy" in compose
    assert 'test: ["CMD", "test", "-f", "/tmp/ready"]' in compose


def test_compose_never_publishes_host_ports_and_tears_down_volumes() -> None:
    compose = Path("docker-compose.integration.yml").read_text(encoding="utf-8")
    runner = Path("run-integration-tests.sh").read_text(encoding="utf-8")
    assert "\n    ports:" not in compose
    assert "down --volumes --remove-orphans" in runner
    assert "trap cleanup EXIT" in runner


def test_compose_does_not_require_removed_iam_migration_token() -> None:
    compose = Path("docker-compose.integration.yml").read_text(encoding="utf-8")
    runner = Path("run-integration-tests.sh").read_text(encoding="utf-8")
    assert "IAM_MIGRATION_INTERNAL_AUTH_TOKEN" not in compose
    assert "IAM_MIGRATION_INTERNAL_AUTH_TOKEN" not in runner


def test_compose_schema_matches_current_iam_user_role_jooq_model() -> None:
    compose = Path("docker-compose.integration.yml").read_text(encoding="utf-8")
    assert "deploy/sqls/1.7.6/1.7.6-full.sql" in compose
    assert "deploy/sqls/1.7.5/1.7.5-full.sql" not in compose


def test_docker_context_excludes_private_runtime_artifacts() -> None:
    patterns = set(Path(".dockerignore").read_text(encoding="utf-8").splitlines())
    assert {"state", "state/**", "reports", "reports/**", "*.sqlite3", "config.toml", ".env"} <= patterns


def test_git_only_ignores_the_operator_config_at_repository_root() -> None:
    patterns = set(Path(".gitignore").read_text(encoding="utf-8").splitlines())

    assert Path("integration/config.toml").is_file()
    assert "/config.toml" in patterns
    assert "config.toml" not in patterns
