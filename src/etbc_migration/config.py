from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from .errors import ConfigError


_SECRET_KEY_FRAGMENTS = ("password", "token", "secret", "credential", "loginpwd")


def reject_secret_config_keys(value: Any, path: tuple[str, ...] = ()) -> None:
    if isinstance(value, dict):
        for raw_key, child in value.items():
            key = str(raw_key)
            normalized = key.replace("-", "").replace("_", "").lower()
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise ConfigError("SECRET_CONFIG_KEY_FORBIDDEN")
            reject_secret_config_keys(child, (*path, key))
    elif isinstance(value, list):
        for child in value:
            reject_secret_config_keys(child, path)


def load_config(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    config_path = Path(path)
    try:
        with config_path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("CONFIG_FILE_INVALID") from error
    reject_secret_config_keys(config)
    return config


def section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError("CONFIG_SECTION_INVALID")
    return value
