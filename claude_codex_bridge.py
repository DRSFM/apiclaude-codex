from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, AsyncIterator, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlsplit


MIN_LITELLM_VERSION = (1, 93, 0)
CODEX_COMPATIBLE_HEADERS = {
    "User-Agent": "codex_cli_rs/apiclaude-bridge",
    "originator": "apiclaude_codex_bridge",
}
CPA_SHIM_API_KEY = "apiclaude-local-auth-shim"
_CLIENT_DISCONNECT_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
)
_HOP_BY_HOP_HEADERS = {
    "authorization",
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class BridgeStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeEndpoint:
    base_url: str
    token: str


class _AuthShimServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        upstream_base_url: str,
        upstream_api_key: str,
        proxy_url: str | None,
    ) -> None:
        super().__init__(server_address, _AuthShimRequestHandler)
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_api_key = upstream_api_key
        self.proxy_url = proxy_url


class _AuthShimRequestHandler(BaseHTTPRequestHandler):
    server: _AuthShimServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except _CLIENT_DISCONNECT_ERRORS:
            return

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:
        request_path = urlsplit(self.path)
        if request_path.path not in ("/responses", "/responses/compact"):
            self.send_error(404)
            return
        authorization = self.headers.get("Authorization", "")
        if not secrets.compare_digest(
            authorization,
            f"Bearer {CPA_SHIM_API_KEY}",
        ):
            self.send_error(401)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024 * 1024:
                raise ValueError("invalid request body length")
            body = self.rfile.read(length)
        except ValueError as exc:
            self.send_error(400, str(exc))
            return

        target_url = (
            f"{self.server.upstream_base_url}{request_path.path}"
            + (f"?{request_path.query}" if request_path.query else "")
        )
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
        }
        headers.update(CODEX_COMPATIBLE_HEADERS)
        headers["Authorization"] = f"Bearer {self.server.upstream_api_key}"
        upstream_request = urllib_request.Request(
            target_url,
            data=body,
            headers=headers,
            method="POST",
        )
        proxy_url = (self.server.proxy_url or "").strip()
        if proxy_url and proxy_url.lower() != "direct":
            opener = urllib_request.build_opener(
                urllib_request.ProxyHandler(
                    {"http": proxy_url, "https": proxy_url}
                )
            )
        else:
            opener = urllib_request.build_opener(urllib_request.ProxyHandler({}))

        try:
            upstream_response = opener.open(upstream_request, timeout=600)
        except urllib_error.HTTPError as exc:
            upstream_response = exc
        except (OSError, urllib_error.URLError) as exc:
            message = str(exc).replace(
                self.server.upstream_api_key,
                "<redacted>",
            )
            self.send_error(502, message)
            return

        try:
            self.send_response(upstream_response.status)
            for name, value in upstream_response.headers.items():
                if name.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = upstream_response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            return
        finally:
            upstream_response.close()
            self.close_connection = True


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        upstream_base_url: str,
        upstream_api_key: str,
        model: str,
        local_token: str,
    ) -> None:
        super().__init__(server_address, _BridgeRequestHandler)
        self.upstream_base_url = upstream_base_url
        self.upstream_api_key = upstream_api_key
        self.model = model
        self.local_token = local_token


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    server: _BridgeServer
    protocol_version = "HTTP/1.1"

    def handle(self) -> None:
        try:
            super().handle()
        except _CLIENT_DISCONNECT_ERRORS:
            return

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/v1/messages":
            self._send_json(
                404,
                {
                    "type": "error",
                    "error": {
                        "type": "not_found_error",
                        "message": "Only /v1/messages is implemented by this prototype.",
                    },
                },
            )
            return
        if not self._authorized():
            self._send_json(
                401,
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "Invalid local bridge token.",
                    },
                },
            )
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 64 * 1024 * 1024:
                raise ValueError("invalid request body length")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict):
                raise ValueError("request body must be an object")
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json(
                400,
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": str(exc)},
                },
            )
            return

        try:
            asyncio.run(self._serve_messages(request))
        except _CLIENT_DISCONNECT_ERRORS:
            return

    def _authorized(self) -> bool:
        bearer = self.headers.get("Authorization", "")
        api_key = self.headers.get("x-api-key", "")
        presented = bearer[7:] if bearer.startswith("Bearer ") else api_key
        return bool(presented) and secrets.compare_digest(
            presented,
            self.server.local_token,
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _safe_error_message(self, exc: Exception) -> str:
        message = str(exc)
        for secret in (self.server.upstream_api_key, self.server.local_token):
            if secret:
                message = message.replace(secret, "<redacted>")
        return message

    async def _serve_messages(self, request: dict[str, Any]) -> None:
        try:
            stream = await _create_anthropic_stream(
                request=request,
                upstream_base_url=self.server.upstream_base_url,
                upstream_api_key=self.server.upstream_api_key,
                model=self.server.model,
            )
        except Exception as exc:
            self._send_json(
                502,
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": (
                            f"Bridge request failed: {self._safe_error_message(exc)}"
                        ),
                    },
                },
            )
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            async for chunk in stream:
                self.wfile.write(chunk)
                self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            return
        except Exception as exc:
            payload = {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": (
                        f"Bridge stream failed: {self._safe_error_message(exc)}"
                    ),
                },
            }
            raw = json.dumps(payload, separators=(",", ":"))
            self.wfile.write(f"event: error\ndata: {raw}\n\n".encode())
            self.wfile.flush()
        finally:
            self.close_connection = True


async def _create_anthropic_stream(
    *,
    request: dict[str, Any],
    upstream_base_url: str,
    upstream_api_key: str,
    model: str,
) -> AsyncIterator[bytes]:
    from litellm import anthropic

    supported = {
        key: request[key]
        for key in (
            "metadata",
            "stop_sequences",
            "system",
            "temperature",
            "thinking",
            "tool_choice",
            "tools",
            "top_k",
            "top_p",
        )
        if key in request
    }
    result = await anthropic.messages.acreate(
        max_tokens=int(request.get("max_tokens") or 8192),
        messages=request.get("messages") or [],
        model=f"openai/{model}",
        stream=True,
        api_key=upstream_api_key,
        api_base=upstream_base_url.rstrip("/"),
        custom_llm_provider="openai",
        drop_params=True,
        extra_headers=CODEX_COMPATIBLE_HEADERS,
        **supported,
    )
    if hasattr(result, "async_anthropic_sse_wrapper"):
        return result.async_anthropic_sse_wrapper()

    async def encode_chunks() -> AsyncIterator[bytes]:
        async for chunk in result:
            if isinstance(chunk, bytes):
                yield chunk
                continue
            if isinstance(chunk, str):
                yield chunk.encode()
                continue
            if hasattr(chunk, "model_dump"):
                chunk = chunk.model_dump(exclude_none=True)
            event_type = chunk.get("type", "message")
            raw = json.dumps(chunk, separators=(",", ":"))
            yield f"event: {event_type}\ndata: {raw}\n\n".encode()

    return encode_chunks()


@contextmanager
def litellm_bridge(
    *,
    upstream_base_url: str,
    upstream_api_key: str,
    model: str,
) -> Iterator[BridgeEndpoint]:
    if importlib.util.find_spec("litellm") is None:
        raise BridgeStartupError(
            "LiteLLM is not installed in this Python environment. "
            "The Codex bridge prototype requires LiteLLM 1.93.0 or newer."
        )
    installed_version = importlib_metadata.version("litellm")
    version_match = re.match(r"^(\d+)\.(\d+)\.(\d+)", installed_version)
    if (
        version_match is None
        or tuple(int(part) for part in version_match.groups())
        < MIN_LITELLM_VERSION
    ):
        raise BridgeStartupError(
            f"LiteLLM {installed_version} is too old for the Codex bridge. "
            "Install LiteLLM 1.93.0 or newer; earlier versions emit invalid "
            "Responses usage serializer warnings or background logging errors."
        )

    local_token = secrets.token_urlsafe(32)
    try:
        server = _BridgeServer(
            ("127.0.0.1", 0),
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            model=model,
            local_token=local_token,
        )
    except OSError as exc:
        raise BridgeStartupError(f"Failed to bind the local bridge: {exc}") from exc

    thread = threading.Thread(
        target=server.serve_forever,
        name="apiclaude-codex-bridge",
        daemon=True,
    )
    thread.start()
    try:
        yield BridgeEndpoint(
            base_url=f"http://127.0.0.1:{server.server_port}",
            token=local_token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _yaml_string(value: str | Path) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _render_cpa_config(
    *,
    host: str,
    port: int,
    auth_dir: Path,
    shim_base_url: str,
    model: str,
) -> str:
    return "\n".join(
        (
            f"host: {_yaml_string(host)}",
            f"port: {port}",
            f"auth-dir: {_yaml_string(auth_dir)}",
            "api-keys: []",
            "debug: false",
            "logging-to-file: false",
            "request-log: false",
            "usage-statistics-enabled: false",
            "disable-image-generation: true",
            "codex-api-key:",
            f"  - api-key: {_yaml_string(CPA_SHIM_API_KEY)}",
            f"    base-url: {_yaml_string(shim_base_url)}",
            "    models:",
            f"      - name: {_yaml_string(model)}",
            f"        alias: {_yaml_string(model)}",
            "        force-mapping: true",
            "",
        )
    )


def _reserve_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _redact_lines(lines: Iterator[str], secrets_to_hide: tuple[str, ...]) -> str:
    output = "\n".join(lines)
    for secret in secrets_to_hide:
        if secret:
            output = output.replace(secret, "<redacted>")
    return output


def _wait_for_cpa(
    *,
    process: subprocess.Popen[str],
    base_url: str,
    logs: deque[str],
    upstream_api_key: str,
    timeout: float = 15,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            detail = _redact_lines(iter(logs), (upstream_api_key,))
            raise BridgeStartupError(
                f"CPA exited before becoming ready (code={process.returncode})."
                + (f"\n{detail}" if detail else "")
            )
        try:
            request = urllib_request.Request(f"{base_url}/v1/models", method="GET")
            with urllib_request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib_error.URLError):
            time.sleep(0.1)
    detail = _redact_lines(iter(logs), (upstream_api_key,))
    raise BridgeStartupError(
        "CPA did not become ready within 15 seconds."
        + (f"\n{detail}" if detail else "")
    )


@contextmanager
def cpa_bridge(
    *,
    upstream_base_url: str,
    upstream_api_key: str,
    model: str,
    cpa_executable: str | Path,
    proxy_url: str | None = None,
) -> Iterator[BridgeEndpoint]:
    executable = Path(cpa_executable).expanduser().resolve()
    if not executable.is_file():
        raise BridgeStartupError(f"CPA executable was not found: {executable}")

    try:
        shim = _AuthShimServer(
            ("127.0.0.1", 0),
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            proxy_url=proxy_url,
        )
    except OSError as exc:
        raise BridgeStartupError(f"Failed to bind the CPA auth shim: {exc}") from exc

    shim_thread = threading.Thread(
        target=shim.serve_forever,
        name="apiclaude-cpa-auth-shim",
        daemon=True,
    )
    shim_thread.start()
    process: subprocess.Popen[str] | None = None
    log_thread: threading.Thread | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="apiclaude-cpa-") as temp:
            temp_dir = Path(temp)
            auth_dir = temp_dir / "auth"
            auth_dir.mkdir()
            cpa_port = _reserve_loopback_port()
            cpa_base_url = f"http://127.0.0.1:{cpa_port}"
            shim_base_url = f"http://127.0.0.1:{shim.server_port}"
            config_path = temp_dir / "config.yaml"
            config_path.write_text(
                _render_cpa_config(
                    host="127.0.0.1",
                    port=cpa_port,
                    auth_dir=auth_dir,
                    shim_base_url=shim_base_url,
                    model=model,
                ),
                encoding="utf-8",
            )

            creationflags = (
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW")
                else 0
            )
            try:
                process = subprocess.Popen(
                    [str(executable), "-config", str(config_path)],
                    cwd=str(executable.parent),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
            except OSError as exc:
                raise BridgeStartupError(f"Failed to start CPA: {exc}") from exc

            logs: deque[str] = deque(maxlen=40)

            def drain_logs() -> None:
                if process is None or process.stdout is None:
                    return
                for line in process.stdout:
                    logs.append(line.rstrip())

            log_thread = threading.Thread(
                target=drain_logs,
                name="apiclaude-cpa-log-drain",
                daemon=True,
            )
            log_thread.start()
            _wait_for_cpa(
                process=process,
                base_url=cpa_base_url,
                logs=logs,
                upstream_api_key=upstream_api_key,
            )
            yield BridgeEndpoint(
                base_url=cpa_base_url,
                token=secrets.token_urlsafe(32),
            )
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if log_thread is not None:
            log_thread.join(timeout=1)
        shim.shutdown()
        shim.server_close()
        shim_thread.join(timeout=5)
