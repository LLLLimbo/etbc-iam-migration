from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests


UPSTREAM = os.environ["IAM_PROXY_UPSTREAM"].rstrip("/")
OBSERVATION_PATH = Path(os.environ.get("IAM_PROXY_OBSERVATIONS", "/observations/requests.jsonl"))
MAX_BODY_BYTES = 16 * 1024 * 1024


def forbidden_password_field(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("_", "").replace("-", "").lower()
            if "password" in normalized or "loginpwd" in normalized:
                return True
            if forbidden_password_field(child):
                return True
    elif isinstance(value, list):
        return any(forbidden_password_field(item) for item in value)
    return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path != "/health":
            self.send_error(404)
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400)
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_error(413)
            return
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_error(400)
            return
        forbidden = forbidden_password_field(payload)
        observation = {
            "migrationBatchId": payload.get("migrationBatchId"),
            "staffSourceIds": [str(item.get("sourceId")) for item in payload.get("staff", [])],
            "forbiddenPasswordField": forbidden,
            "legacyAuthenticationHeadersPresent": any(
                header in self.headers
                for header in ("X-Iam-Internal-Token", "X-Iam-Internal-Caller")
            ),
        }
        OBSERVATION_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with OBSERVATION_PATH.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(observation, sort_keys=True, separators=(",", ":")) + "\n")
        OBSERVATION_PATH.chmod(0o600)
        if forbidden:
            self.send_error(500)
            return
        try:
            response = requests.post(
                UPSTREAM + self.path,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=(5, 90),
            )
        except requests.RequestException:
            self.send_error(502)
            return
        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)


if __name__ == "__main__":
    os.umask(0o077)
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
