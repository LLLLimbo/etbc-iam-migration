from __future__ import annotations

import pytest

from etbc_migration.config import reject_secret_config_keys
from etbc_migration.errors import ConfigError
from etbc_migration.logging_utils import redact_text


def test_logs_redact_secrets_and_personal_identifiers() -> None:
    text = redact_text(
        "email=user@example.com phone=13912345678 id=11010519491231002X token=top-secret",
        secrets=["top-secret"],
    )
    assert "user@example.com" not in text
    assert "13912345678" not in text
    assert "11010519491231002X" not in text
    assert "top-secret" not in text
    assert text.count("[REDACTED]") >= 4


@pytest.mark.parametrize("key", ["password", "db_password", "token", "internalToken", "client_secret"])
def test_config_files_reject_secret_keys(key: str) -> None:
    with pytest.raises(ConfigError, match="SECRET_CONFIG_KEY_FORBIDDEN"):
        reject_secret_config_keys({"etbc": {key: "value"}})
