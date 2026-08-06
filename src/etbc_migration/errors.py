from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    SUCCESS = 0
    LOCAL_VALIDATION = 2
    PARTIAL_FAILURE = 3
    NETWORK_PROTOCOL = 4
    CONFIG = 5


class MigrationError(Exception):
    exit_code = ExitCode.CONFIG

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class LocalValidationError(MigrationError):
    exit_code = ExitCode.LOCAL_VALIDATION


class BatchValidationError(LocalValidationError):
    pass


class PartialFailureError(MigrationError):
    exit_code = ExitCode.PARTIAL_FAILURE

    def __init__(self, code: str = "MIGRATION_PARTIAL_FAILURE") -> None:
        super().__init__(code)


class NetworkProtocolError(MigrationError):
    exit_code = ExitCode.NETWORK_PROTOCOL


class TransportError(NetworkProtocolError):
    pass


class ConfigError(MigrationError):
    exit_code = ExitCode.CONFIG
