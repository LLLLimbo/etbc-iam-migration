from __future__ import annotations

import logging
import re
from collections.abc import Iterable


_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_ID_CARD = re.compile(r"(?<!\d)\d{14,17}[\dXx](?![\dXx])")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:token|password|secret|credential|loginpwd(?:encrypt)?)\s*[=:]\s*[^\s,;]+"
)


def redact_text(value: object, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in sorted((secret for secret in secrets if secret), key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _SECRET_ASSIGNMENT.sub("[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED]", text)
    text = _PHONE.sub("[REDACTED]", text)
    return _ID_CARD.sub("[REDACTED]", text)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), self._secrets)
        record.args = ()
        return True


def configure_logging(verbose: bool = False, secrets: Iterable[str] = ()) -> None:
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
