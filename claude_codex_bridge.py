from __future__ import annotations

import ast
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
from typing import Any, AsyncIterator, Iterator, Sequence
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlsplit, urlunsplit


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
    "x-api-key",
}
_ANTHROPIC_PROXY_PATHS = {
    "/v1/messages",
    "/v1/messages/count_tokens",
    "/v1/models",
}
_ANTHROPIC_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,191}$")
_ANTHROPIC_MODEL_PATH_RE = re.compile(
    r"^/v1/models/[A-Za-z0-9][A-Za-z0-9._:@-]{0,191}$"
)
_ANTHROPIC_1M_MODEL_SUFFIX = "[1m]"
_ANTHROPIC_1M_BETA = "context-1m-2025-08-07"
_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024
_MAX_MODEL_RESPONSE_BYTES = 4 * 1024 * 1024
_MAX_ERROR_RESPONSE_BYTES = 4 * 1024 * 1024


class BridgeStartupError(RuntimeError):
    pass


@dataclass(frozen=True)
class BridgeEndpoint:
    base_url: str
    token: str
    hosted_search_url: str | None = None
    hosted_search_token: str | None = None
    hosted_search_model: str | None = None


def _anthropic_upstream_url(
    base_url: str,
    request_path: str,
    query: str = "",
) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BridgeStartupError("Anthropic upstream must be an HTTP(S) URL")
    base_path = parsed.path.rstrip("/")
    suffix = request_path
    if base_path.endswith("/v1") and suffix.startswith("/v1/"):
        suffix = suffix[3:]
    target_path = f"{base_path}{suffix}"
    return urlunsplit(
        (parsed.scheme, parsed.netloc, target_path, query, "")
    )


def _proxy_opener(proxy_url: str | None) -> urllib_request.OpenerDirector:
    configured = (proxy_url or "").strip()
    proxies = (
        {"http": configured, "https": configured}
        if configured and configured.lower() != "direct"
        else {}
    )
    return urllib_request.build_opener(urllib_request.ProxyHandler(proxies))


def _anthropic_proxy_path_allowed(method: str, path: str) -> bool:
    return path in _ANTHROPIC_PROXY_PATHS or (
        method == "GET" and _ANTHROPIC_MODEL_PATH_RE.fullmatch(path) is not None
    )


def _merge_header_value(
    headers: dict[str, str],
    name: str,
    value: str,
) -> None:
    existing_name = next(
        (header for header in headers if header.lower() == name.lower()),
        None,
    )
    if existing_name is None:
        headers[name] = value
        return
    existing = headers[existing_name]
    values = [item.strip().lower() for item in existing.split(",")]
    if value.lower() not in values:
        headers[existing_name] = f"{existing},{value}"


def _prepare_anthropic_1m_request(
    body: bytes | None,
    headers: dict[str, str],
    *,
    path: str,
    enabled: bool,
) -> bytes | None:
    if not enabled or path not in {
        "/v1/messages",
        "/v1/messages/count_tokens",
    }:
        return body
    _merge_header_value(headers, "anthropic-beta", _ANTHROPIC_1M_BETA)
    if body is None:
        return None
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(payload, dict):
        return body
    model = payload.get("model")
    if not isinstance(model, str) or not model.lower().endswith(
        _ANTHROPIC_1M_MODEL_SUFFIX
    ):
        return body
    normalized_model = model[: -len(_ANTHROPIC_1M_MODEL_SUFFIX)]
    if not normalized_model:
        return body
    payload["model"] = normalized_model
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _iter_upstream_chunks(response: Any) -> Iterator[bytes]:
    content_type = str(response.headers.get("Content-Type", "")).lower()
    reader = (
        response.readline
        if "text/event-stream" in content_type
        else getattr(response, "read1", response.read)
    )
    while True:
        chunk = reader(64 * 1024)
        if not chunk:
            return
        yield chunk


class _AnthropicProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        upstream_base_url: str,
        upstream_api_key: str,
        local_token: str,
        proxy_url: str | None,
        enable_1m: bool,
    ) -> None:
        super().__init__(server_address, _AnthropicProxyRequestHandler)
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_api_key = upstream_api_key
        self.local_token = local_token
        self.proxy_url = proxy_url
        self.enable_1m = enable_1m


class _AnthropicProxyRequestHandler(BaseHTTPRequestHandler):
    server: _AnthropicProxyServer
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

    def do_GET(self) -> None:
        self._proxy_request()

    def do_POST(self) -> None:
        self._proxy_request()

    def _authorized(self) -> bool:
        bearer = self.headers.get("Authorization", "")
        api_key = self.headers.get("x-api-key", "")
        candidates = [
            bearer[7:] if bearer.startswith("Bearer ") else "",
            api_key,
        ]
        return any(
            candidate
            and secrets.compare_digest(candidate, self.server.local_token)
            for candidate in candidates
        )

    def _send_json(self, status: int, message: str) -> None:
        raw = json.dumps(
            {
                "type": "error",
                "error": {"type": "api_error", "message": message},
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _relay_upstream_error(self, response: urllib_error.HTTPError) -> None:
        try:
            raw = response.read(_MAX_ERROR_RESPONSE_BYTES + 1)
            response_headers = list(response.headers.items())
        except OSError:
            raw = b""
            response_headers = []
        finally:
            response.close()
        if len(raw) > _MAX_ERROR_RESPONSE_BYTES:
            self._send_json(
                int(response.code),
                "Anthropic upstream returned an oversized error response.",
            )
            return
        secret = self.server.upstream_api_key
        if secret:
            raw = raw.replace(secret.encode("utf-8"), b"<redacted>")
        self.send_response(int(response.code))
        for name, value in response_headers:
            if name.lower() not in _HOP_BY_HOP_HEADERS:
                safe_value = value.replace(secret, "<redacted>") if secret else value
                self.send_header(name, safe_value)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def _proxy_request(self) -> None:
        request_path = urlsplit(self.path)
        if not _anthropic_proxy_path_allowed(self.command, request_path.path):
            self._send_json(404, "Unsupported Anthropic gateway path.")
            return
        if not self._authorized():
            self._send_json(401, "Invalid local gateway token.")
            return

        body: bytes | None = None
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > _MAX_REQUEST_BODY_BYTES:
                    raise ValueError("invalid request body length")
                body = self.rfile.read(length)
            except ValueError as exc:
                self._send_json(400, str(exc))
                return

        try:
            target_url = _anthropic_upstream_url(
                self.server.upstream_base_url,
                request_path.path,
                request_path.query,
            )
        except BridgeStartupError as exc:
            self._send_json(502, str(exc))
            return
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
        }
        headers["Accept-Encoding"] = "identity"
        headers["Authorization"] = f"Bearer {self.server.upstream_api_key}"
        headers["x-api-key"] = self.server.upstream_api_key
        body = _prepare_anthropic_1m_request(
            body,
            headers,
            path=request_path.path,
            enabled=self.server.enable_1m,
        )
        upstream_request = urllib_request.Request(
            target_url,
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            upstream_response = _proxy_opener(self.server.proxy_url).open(
                upstream_request,
                timeout=600,
            )
        except urllib_error.HTTPError as exc:
            self._relay_upstream_error(exc)
            return
        except (OSError, urllib_error.URLError) as exc:
            message = str(exc).replace(
                self.server.upstream_api_key,
                "<redacted>",
            )
            self._send_json(502, f"Anthropic upstream request failed: {message}")
            return

        try:
            self.send_response(upstream_response.status)
            for name, value in upstream_response.headers.items():
                if name.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in _iter_upstream_chunks(upstream_response):
                self.wfile.write(chunk)
                self.wfile.flush()
        except _CLIENT_DISCONNECT_ERRORS:
            return
        finally:
            upstream_response.close()
            self.close_connection = True


@contextmanager
def anthropic_passthrough_bridge(
    *,
    upstream_base_url: str,
    upstream_api_key: str,
    proxy_url: str | None = None,
    listen_port: int | None = None,
    local_token: str | None = None,
    enable_1m: bool = False,
) -> Iterator[BridgeEndpoint]:
    if listen_port is not None and not 1 <= listen_port <= 65535:
        raise BridgeStartupError("Anthropic gateway port must be between 1 and 65535")
    if not upstream_api_key:
        raise BridgeStartupError("Anthropic upstream API key cannot be empty")
    bridge_token = local_token or secrets.token_urlsafe(32)
    if not bridge_token:
        raise BridgeStartupError("Anthropic gateway token cannot be empty")
    _anthropic_upstream_url(upstream_base_url, "/v1/models")
    try:
        server = _AnthropicProxyServer(
            ("127.0.0.1", listen_port or 0),
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            local_token=bridge_token,
            proxy_url=proxy_url,
            enable_1m=enable_1m,
        )
    except OSError as exc:
        raise BridgeStartupError(
            f"Failed to bind the Anthropic passthrough gateway: {exc}"
        ) from exc

    thread = threading.Thread(
        target=server.serve_forever,
        name="apiclaude-anthropic-passthrough",
        daemon=True,
    )
    thread.start()
    try:
        yield BridgeEndpoint(
            base_url=f"http://127.0.0.1:{server.server_port}",
            token=bridge_token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def discover_anthropic_models(
    *,
    gateway_base_url: str,
    local_token: str,
    max_pages: int = 10,
    max_models: int = 1000,
) -> list[str]:
    if max_pages < 1 or max_models < 1:
        raise ValueError("model discovery limits must be positive")
    models: list[str] = []
    seen_models: set[str] = set()
    seen_cursors: set[str] = set()
    cursor = ""

    for _page in range(max_pages):
        query = urlencode({"after_id": cursor}) if cursor else ""
        target_url = _anthropic_upstream_url(
            gateway_base_url,
            "/v1/models",
            query,
        )
        request = urllib_request.Request(
            target_url,
            headers={
                "Authorization": f"Bearer {local_token}",
                "x-api-key": local_token,
                "Accept": "application/json",
            },
        )
        try:
            with _proxy_opener("direct").open(request, timeout=30) as response:
                raw = response.read(_MAX_MODEL_RESPONSE_BYTES + 1)
        except urllib_error.HTTPError as exc:
            exc.close()
            raise BridgeStartupError(
                f"Claude model discovery failed with HTTP {exc.code}."
            ) from exc
        except (OSError, urllib_error.URLError) as exc:
            raise BridgeStartupError(
                f"Claude model discovery request failed: {exc}"
            ) from exc
        if len(raw) > _MAX_MODEL_RESPONSE_BYTES:
            raise BridgeStartupError("Claude model discovery response exceeded 4 MiB")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise BridgeStartupError(
                "Claude model discovery returned invalid JSON"
            ) from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise BridgeStartupError(
                "Claude model discovery response must contain a data list"
            )
        for item in data:
            model = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
            if (
                model.startswith("claude-")
                and _ANTHROPIC_MODEL_RE.fullmatch(model)
                and model not in seen_models
            ):
                models.append(model)
                seen_models.add(model)
                if len(models) >= max_models:
                    return models
        if payload.get("has_more") is not True:
            break
        cursor = str(payload.get("last_id") or "").strip()
        if not cursor or cursor in seen_cursors:
            raise BridgeStartupError(
                "Claude model discovery returned an invalid pagination cursor"
            )
        seen_cursors.add(cursor)
    else:
        raise BridgeStartupError("Claude model discovery exceeded the page limit")

    if not models:
        raise BridgeStartupError(
            "Claude model discovery found no claude-* models; configure an "
            "explicit list with 'apiclaude desktop-models NODE MODEL...'."
        )
    return models


class _AuthShimServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        upstream_base_url: str,
        upstream_api_key: str,
        proxy_url: str | None,
        hosted_search_token: str,
    ) -> None:
        super().__init__(server_address, _AuthShimRequestHandler)
        self.upstream_base_url = upstream_base_url.rstrip("/")
        self.upstream_api_key = upstream_api_key
        self.proxy_url = proxy_url
        self.hosted_search_token = hosted_search_token


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
        authorized = secrets.compare_digest(
            authorization, f"Bearer {CPA_SHIM_API_KEY}"
        ) or secrets.compare_digest(
            authorization,
            f"Bearer {getattr(self.server, 'hosted_search_token', '')}",
        )
        if not authorized:
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
        body = _normalize_responses_request_body(body)
        tool_name_aliases = _tool_name_aliases_from_request(body)

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
        headers["Accept-Encoding"] = "identity"
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
            content_type = upstream_response.headers.get("Content-Type", "")
            if tool_name_aliases and "text/event-stream" in content_type.lower():
                while True:
                    line = upstream_response.readline()
                    if not line:
                        break
                    self.wfile.write(
                        _rewrite_sse_tool_names(line, tool_name_aliases)
                    )
                    self.wfile.flush()
            elif tool_name_aliases and "application/json" in content_type.lower():
                response_body = upstream_response.read(64 * 1024 * 1024 + 1)
                if len(response_body) > 64 * 1024 * 1024:
                    raise ValueError("upstream response exceeded 64 MiB")
                try:
                    response_json = json.loads(response_body)
                except (UnicodeDecodeError, ValueError):
                    self.wfile.write(response_body)
                else:
                    _rewrite_response_tool_names(
                        response_json,
                        tool_name_aliases,
                    )
                    self.wfile.write(
                        json.dumps(
                            response_json,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    )
                self.wfile.flush()
            else:
                for chunk in _iter_upstream_chunks(upstream_response):
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
    local_token: str,
    route_model: str | None = None,
    extra_models: Sequence[str] | None = None,
) -> str:
    advertised_model = route_model or model
    routes = [(model, advertised_model)]
    seen_aliases = {advertised_model}
    for extra_model in extra_models or ():
        cleaned = str(extra_model).strip()
        if not cleaned or cleaned in seen_aliases:
            continue
        routes.append((cleaned, cleaned))
        seen_aliases.add(cleaned)

    lines = [
        f"host: {_yaml_string(host)}",
        f"port: {port}",
        f"auth-dir: {_yaml_string(auth_dir)}",
        "api-keys:",
        f"  - {_yaml_string(local_token)}",
        "debug: false",
        "logging-to-file: false",
        "request-log: false",
        "usage-statistics-enabled: false",
        "disable-image-generation: true",
        "codex-api-key:",
        f"  - api-key: {_yaml_string(CPA_SHIM_API_KEY)}",
        f"    base-url: {_yaml_string(shim_base_url)}",
        "    models:",
    ]
    for upstream_model, alias in routes:
        lines.extend(
            (
                f"      - name: {_yaml_string(upstream_model)}",
                f"        alias: {_yaml_string(alias)}",
                "        force-mapping: true",
            )
        )
    lines.append("")
    return "\n".join(lines)


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


def _tool_name_aliases_from_request(body: bytes) -> dict[str, str]:
    """Map a unique shortened MCP tool name back to its declared full name."""

    try:
        request = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if not isinstance(request, dict):
        return {}
    tools = request.get("tools")
    if not isinstance(tools, list):
        return {}

    declared_names = {
        str(tool.get("name") or "")
        for tool in tools
        if isinstance(tool, dict) and str(tool.get("name") or "")
    }
    candidates: dict[str, set[str]] = {}
    for full_name in declared_names:
        if not full_name.startswith("mcp__") or "__" not in full_name[5:]:
            continue
        short_name = full_name.rsplit("__", 1)[-1]
        if not short_name or short_name == full_name:
            continue
        candidates.setdefault(short_name, set()).add(full_name)

    aliases: dict[str, str] = {}
    for short_name, full_names in candidates.items():
        if len(full_names) == 1 and short_name not in declared_names:
            aliases[short_name] = next(iter(full_names))
    return aliases


def _flatten_cpa_function_output(output: Any) -> str | None:
    """Extract text from CPA's Anthropic tool-result block list representation."""

    parsed: Any = output
    if isinstance(output, str):
        candidate = output.strip()
        if (
            len(candidate) > 2 * 1024 * 1024
            or not candidate.startswith("[")
            or "input_text" not in candidate
        ):
            return None
        try:
            parsed = json.loads(candidate)
        except ValueError:
            try:
                parsed = ast.literal_eval(candidate)
            except (MemoryError, RecursionError, SyntaxError, ValueError):
                return None
    if not isinstance(parsed, list) or not parsed:
        return None
    texts: list[str] = []
    for block in parsed:
        if (
            not isinstance(block, dict)
            or block.get("type") not in {"input_text", "output_text", "text"}
            or not isinstance(block.get("text"), str)
        ):
            return None
        texts.append(block["text"])
    return "\n".join(texts)


def _normalize_responses_function_outputs(value: Any) -> bool:
    """Flatten CPA block arrays before forwarding function output to Responses."""

    changed = False
    if isinstance(value, dict):
        if value.get("type") == "function_call_output":
            flattened = _flatten_cpa_function_output(value.get("output"))
            if flattened is not None:
                value["output"] = flattened
                changed = True
        for child in value.values():
            if _normalize_responses_function_outputs(child):
                changed = True
    elif isinstance(value, list):
        for child in value:
            if _normalize_responses_function_outputs(child):
                changed = True
    return changed


def _normalize_responses_request_body(body: bytes) -> bytes:
    try:
        request = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return body
    if not _normalize_responses_function_outputs(request):
        return body
    return json.dumps(
        request,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _rewrite_response_tool_names(value: Any, aliases: dict[str, str]) -> bool:
    """Restore MCP names in Responses function-call output objects in place."""

    changed = False
    if isinstance(value, dict):
        if value.get("type") == "function_call":
            name = value.get("name")
            replacement = aliases.get(name) if isinstance(name, str) else None
            if replacement:
                value["name"] = replacement
                changed = True
        for child in value.values():
            if _rewrite_response_tool_names(child, aliases):
                changed = True
    elif isinstance(value, list):
        for child in value:
            if _rewrite_response_tool_names(child, aliases):
                changed = True
    return changed


def _rewrite_sse_tool_names(line: bytes, aliases: dict[str, str]) -> bytes:
    if not aliases or not line.startswith(b"data:"):
        return line
    raw_payload = line[5:].strip()
    if not raw_payload or raw_payload == b"[DONE]":
        return line
    try:
        payload = json.loads(raw_payload)
    except (UnicodeDecodeError, ValueError):
        return line
    if not _rewrite_response_tool_names(payload, aliases):
        return line
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return b"data: " + encoded + (b"\r\n" if line.endswith(b"\r\n") else b"\n")


def _wait_for_cpa(
    *,
    process: subprocess.Popen[str],
    base_url: str,
    logs: deque[str],
    upstream_api_key: str,
    local_token: str,
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
            request = urllib_request.Request(
                f"{base_url}/v1/models",
                headers={"Authorization": f"Bearer {local_token}"},
                method="GET",
            )
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
    listen_port: int | None = None,
    local_token: str | None = None,
    route_model: str | None = None,
    extra_models: Sequence[str] | None = None,
) -> Iterator[BridgeEndpoint]:
    executable = Path(cpa_executable).expanduser().resolve()
    if not executable.is_file():
        raise BridgeStartupError(f"CPA executable was not found: {executable}")
    if listen_port is not None and not 1 <= listen_port <= 65535:
        raise BridgeStartupError("CPA listen port must be between 1 and 65535")
    bridge_token = local_token or secrets.token_urlsafe(32)
    if not bridge_token:
        raise BridgeStartupError("CPA local token cannot be empty")
    hosted_search_token = secrets.token_urlsafe(32)

    try:
        shim = _AuthShimServer(
            ("127.0.0.1", 0),
            upstream_base_url=upstream_base_url,
            upstream_api_key=upstream_api_key,
            proxy_url=proxy_url,
            hosted_search_token=hosted_search_token,
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
            cpa_port = listen_port or _reserve_loopback_port()
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
                    local_token=bridge_token,
                    route_model=route_model,
                    extra_models=extra_models,
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
                local_token=bridge_token,
            )
            yield BridgeEndpoint(
                base_url=cpa_base_url,
                token=bridge_token,
                hosted_search_url=f"{shim_base_url}/responses",
                hosted_search_token=hosted_search_token,
                hosted_search_model=model,
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
        if process is not None and process.stdout is not None:
            process.stdout.close()
        shim.shutdown()
        shim.server_close()
        shim_thread.join(timeout=5)
