"""Loopback Responses proxy that supplies vision context to text-only models."""

from __future__ import annotations

import base64
import copy
import hmac
import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


GEMINI_INTERACTIONS_URL = (
    "https://generativelanguage.googleapis.com/v1beta/interactions"
)
MAX_PROXY_REQUEST_BYTES = 32 * 1024 * 1024
MAX_GEMINI_REQUEST_BYTES = 20 * 1024 * 1024
MAX_GEMINI_OUTPUT_BYTES = 512 * 1024
MAX_VISION_PROMPT_CHARS = 16_000
SUPPORTED_IMAGE_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/heic",
    "image/heif",
}
_DATA_URL_RE = re.compile(
    r"^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=]+)$",
    re.IGNORECASE,
)
_HOP_BY_HOP_HEADERS = {
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
_VISION_SYSTEM_INSTRUCTION = (
    "You are a visual perception adapter for a downstream coding agent. "
    "Describe only what is visibly supported by the supplied image or images. "
    "Preserve exact visible text, error messages, UI hierarchy, layout, colors, "
    "spatial relationships, charts, code, and other details relevant to the "
    "user's request. Clearly mark uncertainty and never invent hidden content. "
    "Return concise but sufficiently complete plain text, not markdown fences."
)


class VisionProxyError(RuntimeError):
    """A safe, user-facing error raised by the local vision adapter."""


@dataclass(frozen=True)
class VisionImage:
    mime_type: str
    source: str


@dataclass(frozen=True)
class VisionProxyEndpoint:
    base_url: str
    port: int


def _decode_data_url(image: VisionImage) -> tuple[str, str]:
    match = _DATA_URL_RE.fullmatch(image.source)
    if not match:
        raise VisionProxyError("vision fallback supports inline Base64 images only")
    mime_type = match.group("mime").lower()
    if mime_type != image.mime_type.lower():
        raise VisionProxyError("image MIME type does not match its data URL")
    if mime_type not in SUPPORTED_IMAGE_MIME_TYPES:
        raise VisionProxyError(f"unsupported image MIME type: {mime_type}")
    encoded = match.group("data")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise VisionProxyError("image contains invalid Base64 data") from exc
    if not decoded:
        raise VisionProxyError("image data is empty")
    return mime_type, encoded


def _read_limited(response: Any, limit: int) -> bytes:
    body = response.read(limit + 1)
    if len(body) > limit:
        raise VisionProxyError("Gemini response exceeded the local size limit")
    return body


def _gemini_error_detail(exc: HTTPError) -> str:
    try:
        raw = exc.read(64 * 1024)
        payload = json.loads(raw.decode("utf-8-sig"))
        detail = payload.get("error", {}).get("message")
        if isinstance(detail, str) and detail.strip():
            return detail.strip()[:500]
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass
    return f"HTTP {exc.code}"


def _extract_interaction_text(payload: dict[str, Any]) -> str:
    if payload.get("status") != "completed":
        raise VisionProxyError(
            f"Gemini interaction did not complete (status={payload.get('status')!r})"
        )
    parts: list[str] = []
    for step in payload.get("steps") or []:
        if not isinstance(step, dict) or step.get("type") != "model_output":
            continue
        for item in step.get("content") or []:
            if not isinstance(item, dict) or item.get("type") != "text":
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    result = "\n\n".join(parts).strip()
    if not result:
        raise VisionProxyError("Gemini returned no visual description")
    return result


def request_gemini_vision(
    *,
    api_key: str,
    model: str,
    user_prompt: str,
    images: list[VisionImage],
    open_request: Callable[..., Any] | None = None,
    timeout: float = 30,
) -> str:
    """Describe one Responses request's images through Gemini Interactions."""

    if not api_key.strip():
        raise VisionProxyError("Gemini API key is empty")
    if not model.strip():
        raise VisionProxyError("Gemini vision model is empty")
    if not images:
        raise VisionProxyError("no images were supplied to the vision fallback")

    interaction_input: list[dict[str, str]] = [
        {
            "type": "text",
            "text": (
                "Downstream user request:\n"
                + (user_prompt.strip() or "Describe the attached image accurately.")[
                    :MAX_VISION_PROMPT_CHARS
                ]
            ),
        }
    ]
    for index, image in enumerate(images, start=1):
        mime_type, encoded = _decode_data_url(image)
        interaction_input.extend(
            [
                {"type": "text", "text": f"Image {index}:"},
                {
                    "type": "image",
                    "data": encoded,
                    "mime_type": mime_type,
                },
            ]
        )

    payload = {
        "model": model.strip(),
        "input": interaction_input,
        "system_instruction": _VISION_SYSTEM_INSTRUCTION,
        "response_format": {"type": "text"},
        "generation_config": {
            "thinking_level": "minimal",
            "max_output_tokens": 8192,
        },
        "store": False,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(body) >= MAX_GEMINI_REQUEST_BYTES:
        raise VisionProxyError("combined image request exceeds Gemini's 20 MB limit")

    request = Request(
        GEMINI_INTERACTIONS_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-goog-api-key": api_key.strip(),
        },
        method="POST",
    )
    opener = open_request or urlopen
    try:
        with opener(request, timeout=timeout) as response:
            raw = _read_limited(response, MAX_GEMINI_OUTPUT_BYTES)
    except HTTPError as exc:
        raise VisionProxyError(
            f"Gemini vision request failed: {_gemini_error_detail(exc)}"
        ) from exc
    except URLError as exc:
        raise VisionProxyError(f"Gemini vision request failed: {exc.reason}") from exc
    except OSError as exc:
        raise VisionProxyError(f"Gemini vision request failed: {exc}") from exc

    try:
        response_payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisionProxyError("Gemini returned invalid JSON") from exc
    if not isinstance(response_payload, dict):
        raise VisionProxyError("Gemini returned an invalid interaction object")
    return _extract_interaction_text(response_payload)


def _collect_input_images(value: Any, images: list[VisionImage]) -> None:
    if isinstance(value, list):
        for item in value:
            _collect_input_images(item, images)
        return
    if not isinstance(value, dict):
        return
    if value.get("type") == "input_image":
        source = value.get("image_url") or value.get("url")
        if isinstance(source, dict):
            source = source.get("url")
        if not isinstance(source, str):
            raise VisionProxyError("Responses image input is missing image_url")
        match = _DATA_URL_RE.match(source)
        mime_type = match.group("mime").lower() if match else "application/octet-stream"
        images.append(VisionImage(mime_type=mime_type, source=source))
        return
    for child in value.values():
        _collect_input_images(child, images)


def _latest_user_text(payload: dict[str, Any]) -> str:
    inputs = payload.get("input")
    if not isinstance(inputs, list):
        return inputs[:MAX_VISION_PROMPT_CHARS] if isinstance(inputs, str) else ""
    for item in reversed(inputs):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        texts: list[str] = []
        content = item.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"input_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        texts.append(text)
        return "\n".join(texts)[:MAX_VISION_PROMPT_CHARS]
    return ""


_REMOVE = object()


def _replace_images(value: Any, description: str, inserted: list[bool]) -> Any:
    if isinstance(value, list):
        result = []
        for item in value:
            transformed = _replace_images(item, description, inserted)
            if transformed is not _REMOVE:
                result.append(transformed)
        return result
    if not isinstance(value, dict):
        return value
    if value.get("type") == "input_image":
        if inserted[0]:
            return _REMOVE
        inserted[0] = True
        return {
            "type": "input_text",
            "text": (
                "[Visual context supplied by the configured Gemini vision "
                "assistant; the original image was not sent to the main model.]\n"
                f"{description}\n[End visual context]"
            ),
        }
    return {
        key: transformed
        for key, child in value.items()
        if (transformed := _replace_images(child, description, inserted)) is not _REMOVE
    }


def enrich_responses_request(
    payload: dict[str, Any],
    *,
    analyze: Callable[[str, list[VisionImage]], str],
) -> tuple[dict[str, Any], bool]:
    """Replace Responses input_image items with one model-visible description."""

    images: list[VisionImage] = []
    _collect_input_images(payload.get("input"), images)
    if not images:
        return payload, False
    description = analyze(_latest_user_text(payload), images).strip()
    if not description:
        raise VisionProxyError("vision assistant returned an empty description")
    cloned = copy.deepcopy(payload)
    replaced = _replace_images(cloned, description, [False])
    if not isinstance(replaced, dict):
        raise VisionProxyError("failed to rebuild the Responses request")
    return replaced, True


class _VisionProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _VisionProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def vision_server(self) -> _VisionProxyServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, *_args: Any) -> None:
        return None

    def _write_json_error(self, status: int, message: str) -> None:
        body = json.dumps(
            {"error": {"message": message, "type": "vision_proxy_error"}},
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _authorized_upstream_request(self) -> bool:
        expected = f"Bearer {self.vision_server.upstream_api_key}"
        actual = self.headers.get("Authorization", "")
        return hmac.compare_digest(actual, expected)

    def _is_allowed_path(self) -> bool:
        path = urlsplit(self.path).path
        prefix = self.vision_server.upstream_path_prefix
        return path == prefix or path.startswith(prefix.rstrip("/") + "/")

    def _health(self) -> None:
        token = self.headers.get("X-ApiCodex-Vision-Control", "")
        if not hmac.compare_digest(token, self.vision_server.control_token):
            self._write_json_error(401, "invalid vision worker control token")
            return
        body = json.dumps(
            {
                "status": "ok",
                "profile": self.vision_server.profile_id,
                "model": self.vision_server.gemini_model,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if urlsplit(self.path).path == "/__apicodex_vision__/health":
            self._health()
            return
        self._forward(None)

    def do_POST(self) -> None:
        content_encoding = self.headers.get("Content-Encoding", "").strip().lower()
        if content_encoding and content_encoding != "identity":
            self._write_json_error(
                415,
                "compressed requests are disabled for vision-enabled profiles",
            )
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._write_json_error(400, "invalid Content-Length")
            return
        if length <= 0 or length > MAX_PROXY_REQUEST_BYTES:
            self._write_json_error(413, "request body exceeds the local size limit")
            return
        body = self.rfile.read(length)
        if urlsplit(self.path).path.endswith("/responses"):
            try:
                payload = json.loads(body.decode("utf-8-sig"))
                if not isinstance(payload, dict):
                    raise VisionProxyError("Responses body must be a JSON object")
                payload, changed = enrich_responses_request(
                    payload,
                    analyze=lambda prompt, images: request_gemini_vision(
                        api_key=self.vision_server.gemini_api_key,
                        model=self.vision_server.gemini_model,
                        user_prompt=prompt,
                        images=images,
                    ),
                )
                if changed:
                    body = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
            except (UnicodeError, json.JSONDecodeError, VisionProxyError) as exc:
                self._write_json_error(502, str(exc))
                return
        self._forward(body)

    def _forward(self, body: bytes | None) -> None:
        if not self._is_allowed_path():
            self._write_json_error(404, "path is outside the configured upstream API")
            return
        if not self._authorized_upstream_request():
            self._write_json_error(401, "invalid upstream bearer token")
            return
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in _HOP_BY_HOP_HEADERS
            and name.lower() != "content-encoding"
        }
        headers["Authorization"] = f"Bearer {self.vision_server.upstream_api_key}"
        headers["Accept-Encoding"] = "identity"
        target = self.vision_server.upstream_origin + self.path
        request = Request(target, data=body, headers=headers, method=self.command)
        try:
            response = urlopen(request, timeout=self.vision_server.upstream_timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            self._write_json_error(502, f"upstream request failed: {reason}")
            return

        try:
            self.send_response(response.status)
            for name, value in response.headers.items():
                if name.lower() not in _HOP_BY_HOP_HEADERS:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass
        finally:
            response.close()
            self.close_connection = True


@contextmanager
def vision_proxy(
    *,
    upstream_base_url: str,
    upstream_api_key: str,
    gemini_api_key: str,
    gemini_model: str,
    control_token: str,
    profile_id: str = "test",
    host: str = "127.0.0.1",
    port: int = 0,
    upstream_timeout: float = 120,
) -> Iterator[VisionProxyEndpoint]:
    """Run a loopback-only proxy for one configured Codex API Profile."""

    parsed = urlsplit(upstream_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VisionProxyError("upstream base URL must be absolute HTTP or HTTPS")
    if host not in {"127.0.0.1", "::1"}:
        raise VisionProxyError("vision proxy must listen on loopback")
    if not upstream_api_key or not gemini_api_key or not control_token:
        raise VisionProxyError("vision proxy credentials cannot be empty")

    path_prefix = parsed.path.rstrip("/") or "/"
    server = _VisionProxyServer((host, port), _VisionProxyHandler)
    server.upstream_origin = f"{parsed.scheme}://{parsed.netloc}"
    server.upstream_path_prefix = path_prefix
    server.upstream_api_key = upstream_api_key
    server.gemini_api_key = gemini_api_key
    server.gemini_model = gemini_model
    server.control_token = control_token
    server.profile_id = profile_id
    server.upstream_timeout = upstream_timeout
    actual_port = int(server.server_address[1])
    proxy_path = "" if path_prefix == "/" else path_prefix
    endpoint = VisionProxyEndpoint(
        base_url=f"http://{host}:{actual_port}{proxy_path}",
        port=actual_port,
    )
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"apicodex-vision-{profile_id}",
        daemon=True,
    )
    thread.start()
    try:
        yield endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
