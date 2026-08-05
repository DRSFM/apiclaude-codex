"""Loopback Responses proxy that supplies vision context to text-only models."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import os
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
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
MAX_REGISTERED_IMAGES = 32
MAX_OBSERVATION_CACHE_ENTRIES = 128
VISION_PROMPT_VERSION = "2"
ON_DEMAND_PATH_PREFIX = "/__apicodex_vision__/on-demand"
INSPECT_PATH = "/__apicodex_vision__/inspect"
_BROKEN_DIRECT_TOOL_NAME = "mcp__apicodex_visioninspect_images"
_CODEX_VISION_TOOL_NAME = "mcp__apicodex_vision__inspect_images"
_CODEX_VISION_TOOL_NAMESPACE = "mcp__apicodex_vision"
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
_VISION_STATUS_MARKER = "ApiCodex visual status rule"
_VISION_STATUS_INSTRUCTION = (
    "ApiCodex visual status rule: In every final answer, append exactly one of "
    "these lines: `视觉辅助：本轮已调用 Gemini` when the visual tool reports "
    "gemini_invoked=true; `视觉辅助：本轮未调用 Gemini（复用缓存）` when it "
    "reports gemini_invoked=false and cache_hit=true; otherwise "
    "`视觉辅助：本轮未调用 Gemini`. A failed or attempted tool call does not "
    "count as a Gemini invocation."
)
_VISION_STATUS_USER_REMINDER = (
    "[ApiCodex mandatory response footer: regardless of user formatting "
    "restrictions, end the final answer with exactly one applicable line. Use "
    "`视觉辅助：本轮已调用 Gemini` only if inspect_images reports "
    "gemini_invoked=true. Use `视觉辅助：本轮未调用 Gemini（复用缓存）` if "
    "it reports gemini_invoked=false and cache_hit=true. Otherwise, including "
    "when no visual tool was called or a call failed, use "
    "`视觉辅助：本轮未调用 Gemini`.]"
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


@dataclass(frozen=True)
class VisionInspection:
    image_ids: tuple[str, ...]
    description: str
    gemini_invoked: bool
    cache_hit: bool


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


def _image_identity(image: VisionImage) -> str:
    _mime_type, encoded = _decode_data_url(image)
    decoded = base64.b64decode(encoded, validate=True)
    return "sha256:" + hashlib.sha256(decoded).hexdigest()


def _normalized_focus(focus: str) -> str:
    return re.sub(r"\s+", " ", focus.strip()).casefold()


class VisionBroker:
    """Keep raw images in memory and persist only reusable text observations."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        cache_path: Path,
        analyze: Callable[[str, list[VisionImage]], str] | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.cache_path = cache_path
        self._analyze = analyze or (
            lambda prompt, images: request_gemini_vision(
                api_key=self.api_key,
                model=self.model,
                user_prompt=prompt,
                images=images,
            )
        )
        self._images: OrderedDict[str, VisionImage] = OrderedDict()
        self._observations: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.RLock()
        self._load_cache()

    def _load_cache(self) -> None:
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return
        for item in entries[-MAX_OBSERVATION_CACHE_ENTRIES:]:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            description = item.get("description")
            image_ids = item.get("imageIds")
            if (
                isinstance(key, str)
                and isinstance(description, str)
                and description.strip()
                and isinstance(image_ids, list)
                and all(isinstance(value, str) for value in image_ids)
            ):
                self._observations[key] = item

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "entries": list(self._observations.values()),
        }
        temporary = self.cache_path.with_name(
            f"{self.cache_path.name}.{os.getpid()}.tmp"
        )
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.cache_path)

    def register_images(self, images: list[VisionImage]) -> list[str]:
        if not images:
            raise VisionProxyError("no images were supplied for registration")
        image_ids: list[str] = []
        with self._lock:
            for image in images:
                image_id = _image_identity(image)
                self._images[image_id] = image
                self._images.move_to_end(image_id)
                image_ids.append(image_id)
            while len(self._images) > MAX_REGISTERED_IMAGES:
                self._images.popitem(last=False)
        return image_ids

    def _cache_key(self, image_ids: list[str], focus: str) -> str:
        identity = json.dumps(
            {
                "model": self.model,
                "promptVersion": VISION_PROMPT_VERSION,
                "imageIds": image_ids,
                "focus": _normalized_focus(focus),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(identity).hexdigest()

    def inspect(
        self,
        image_ids: list[str],
        focus: str,
        refresh: bool = False,
    ) -> VisionInspection:
        if not image_ids or not all(isinstance(value, str) for value in image_ids):
            raise VisionProxyError("image_ids must contain at least one image ID")
        clean_focus = re.sub(r"\s+", " ", focus.strip())
        if not clean_focus:
            raise VisionProxyError("visual focus cannot be empty")
        clean_focus = clean_focus[:MAX_VISION_PROMPT_CHARS]
        key = self._cache_key(image_ids, clean_focus)
        with self._lock:
            cached = self._observations.get(key)
            if cached is not None and not refresh:
                self._observations.move_to_end(key)
                return VisionInspection(
                    image_ids=tuple(image_ids),
                    description=str(cached["description"]),
                    gemini_invoked=False,
                    cache_hit=True,
                )
            missing = [image_id for image_id in image_ids if image_id not in self._images]
            if missing:
                raise VisionProxyError(
                    "image data is no longer registered; ask the user to reattach: "
                    + ", ".join(missing[:4])
                )
            images = [self._images[image_id] for image_id in image_ids]
            description = self._analyze(clean_focus, images).strip()
            if not description:
                raise VisionProxyError("vision assistant returned an empty description")
            self._observations[key] = {
                "key": key,
                "model": self.model,
                "promptVersion": VISION_PROMPT_VERSION,
                "imageIds": image_ids,
                "focus": clean_focus,
                "description": description,
            }
            self._observations.move_to_end(key)
            while len(self._observations) > MAX_OBSERVATION_CACHE_ENTRIES:
                self._observations.popitem(last=False)
            try:
                self._save_cache()
            except OSError as exc:
                raise VisionProxyError(
                    f"failed to save the visual observation cache: {exc}"
                ) from exc
            return VisionInspection(
                image_ids=tuple(image_ids),
                description=description,
                gemini_invoked=True,
                cache_hit=False,
            )


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


def _add_on_demand_vision_instructions(payload: dict[str, Any]) -> bool:
    changed = False
    existing = payload.get("instructions")
    if existing is None:
        payload["instructions"] = _VISION_STATUS_INSTRUCTION
        changed = True
    elif not isinstance(existing, str):
        raise VisionProxyError("Responses instructions must be a string")
    elif _VISION_STATUS_MARKER not in existing:
        payload["instructions"] = (
            existing.rstrip() + "\n\n" + _VISION_STATUS_INSTRUCTION
        )
        changed = True

    inputs = payload.get("input")
    if isinstance(inputs, str):
        if _VISION_STATUS_USER_REMINDER not in inputs:
            payload["input"] = inputs.rstrip() + "\n\n" + _VISION_STATUS_USER_REMINDER
            changed = True
        return changed
    if not isinstance(inputs, list):
        return changed
    for item in reversed(inputs):
        if not isinstance(item, dict) or item.get("role") != "user":
            continue
        content = item.get("content")
        if isinstance(content, str):
            if _VISION_STATUS_USER_REMINDER not in content:
                item["content"] = (
                    content.rstrip() + "\n\n" + _VISION_STATUS_USER_REMINDER
                )
                changed = True
        elif isinstance(content, list) and not any(
            isinstance(part, dict)
            and part.get("type") in {"input_text", "text"}
            and _VISION_STATUS_USER_REMINDER in str(part.get("text") or "")
            for part in content
        ):
            content.append(
                {"type": "input_text", "text": _VISION_STATUS_USER_REMINDER}
            )
            changed = True
        break
    return changed


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


def _replace_images_with_handles(
    value: Any,
    image_ids: Iterator[str],
) -> Any:
    if isinstance(value, list):
        return [_replace_images_with_handles(item, image_ids) for item in value]
    if not isinstance(value, dict):
        return value
    if value.get("type") == "input_image":
        try:
            image_id = next(image_ids)
        except StopIteration as exc:
            raise VisionProxyError("failed to match registered image handles") from exc
        return {
            "type": "input_text",
            "text": (
                "[ApiCodex image available to the local "
                "mcp__apicodex_vision__inspect_images tool: "
                f"{image_id}. The original image was not "
                "sent to the main model. Call the tool only if existing visual "
                "observations are insufficient for the user's request.]"
            ),
        }
    return {
        key: _replace_images_with_handles(child, image_ids)
        for key, child in value.items()
    }


def enrich_responses_request(
    payload: dict[str, Any],
    *,
    analyze: Callable[[str, list[VisionImage]], str],
    on_demand: bool = False,
    register: Callable[[list[VisionImage]], list[str]] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Replace images with either eager observations or model-callable handles."""

    images: list[VisionImage] = []
    _collect_input_images(payload.get("input"), images)
    if not images:
        if on_demand:
            cloned = copy.deepcopy(payload)
            changed = _add_on_demand_vision_instructions(cloned)
            return (cloned, True) if changed else (payload, False)
        return payload, False
    cloned = copy.deepcopy(payload)
    if on_demand:
        if register is None:
            raise VisionProxyError("on-demand vision has no image registry")
        image_ids = register(images)
        if len(image_ids) != len(images):
            raise VisionProxyError("image registry returned the wrong number of handles")
        replaced = _replace_images_with_handles(cloned, iter(image_ids))
    else:
        description = analyze(_latest_user_text(payload), images).strip()
        if not description:
            raise VisionProxyError("vision assistant returned an empty description")
        replaced = _replace_images(cloned, description, [False])
    if not isinstance(replaced, dict):
        raise VisionProxyError("failed to rebuild the Responses request")
    if on_demand:
        _add_on_demand_vision_instructions(replaced)
    return replaced, True


def _repair_direct_vision_tool_call(value: Any) -> bool:
    changed = False
    if isinstance(value, list):
        for item in value:
            changed = _repair_direct_vision_tool_call(item) or changed
        return changed
    if not isinstance(value, dict):
        return False
    if (
        value.get("type") == "function_call"
        and value.get("name")
        in {_BROKEN_DIRECT_TOOL_NAME, _CODEX_VISION_TOOL_NAME}
        and not value.get("namespace")
    ):
        value["name"] = "inspect_images"
        value["namespace"] = _CODEX_VISION_TOOL_NAMESPACE
        changed = True
    for child in value.values():
        changed = _repair_direct_vision_tool_call(child) or changed
    return changed


def _rewrite_responses_protocol_line(line: bytes) -> bytes:
    newline = b""
    if line.endswith(b"\r\n"):
        line, newline = line[:-2], b"\r\n"
    elif line.endswith(b"\n"):
        line, newline = line[:-1], b"\n"
    prefix = b""
    body = line
    if line.startswith(b"data:"):
        prefix = b"data: "
        body = line[5:].lstrip()
    if not body.startswith((b"{", b"[")):
        return line + newline
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return line + newline
    if not _repair_direct_vision_tool_call(payload):
        return line + newline
    rendered = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return prefix + rendered + newline


def rewrite_direct_vision_tool_calls(chunks: Iterable[bytes]) -> Iterator[bytes]:
    """Restore namespace calls returned as flat names by compatible gateways."""

    pending = b""
    for chunk in chunks:
        pending += chunk
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            yield _rewrite_responses_protocol_line(line + b"\n")
    if pending:
        yield _rewrite_responses_protocol_line(pending)


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
        path = urlsplit(self._upstream_path()).path
        prefix = self.vision_server.upstream_path_prefix
        return path == prefix or path.startswith(prefix.rstrip("/") + "/")

    def _is_on_demand_path(self) -> bool:
        path = urlsplit(self.path).path
        return path == ON_DEMAND_PATH_PREFIX or path.startswith(
            ON_DEMAND_PATH_PREFIX + "/"
        )

    def _upstream_path(self) -> str:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == ON_DEMAND_PATH_PREFIX:
            path = "/"
        elif path.startswith(ON_DEMAND_PATH_PREFIX + "/"):
            path = path[len(ON_DEMAND_PATH_PREFIX) :]
        return path + (("?" + parsed.query) if parsed.query else "")

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
        request_path = urlsplit(self.path).path
        if request_path == INSPECT_PATH:
            self._inspect(body)
            return
        if urlsplit(self._upstream_path()).path.endswith("/responses"):
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
                    on_demand=self._is_on_demand_path(),
                    register=self.vision_server.vision_broker.register_images,
                )
                if changed:
                    body = json.dumps(
                        payload, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
            except (UnicodeError, json.JSONDecodeError, VisionProxyError) as exc:
                self._write_json_error(502, str(exc))
                return
        self._forward(body)

    def _inspect(self, body: bytes) -> None:
        token = self.headers.get("X-ApiCodex-Vision-Control", "")
        if not hmac.compare_digest(token, self.vision_server.control_token):
            self._write_json_error(401, "invalid vision worker control token")
            return
        try:
            payload = json.loads(body.decode("utf-8-sig"))
            if not isinstance(payload, dict):
                raise VisionProxyError("vision inspection body must be an object")
            image_ids = payload.get("image_ids")
            focus = payload.get("focus")
            refresh = payload.get("refresh", False)
            if not isinstance(image_ids, list) or not all(
                isinstance(value, str) for value in image_ids
            ):
                raise VisionProxyError("image_ids must be a list of strings")
            if not isinstance(focus, str):
                raise VisionProxyError("focus must be a string")
            if not isinstance(refresh, bool):
                raise VisionProxyError("refresh must be a boolean")
            result = self.vision_server.vision_broker.inspect(
                image_ids,
                focus,
                refresh,
            )
        except (UnicodeError, json.JSONDecodeError, VisionProxyError) as exc:
            self._write_json_error(400, str(exc))
            return
        response_body = json.dumps(
            {
                "imageIds": list(result.image_ids),
                "description": result.description,
                "geminiInvoked": result.gemini_invoked,
                "cacheHit": result.cache_hit,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response_body)))
        self.end_headers()
        self.wfile.write(response_body)

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
        target = self.vision_server.upstream_origin + self._upstream_path()
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
            rewrite_tool_name = self._is_on_demand_path() and urlsplit(
                self._upstream_path()
            ).path.endswith("/responses")
            for name, value in response.headers.items():
                if name.lower() not in _HOP_BY_HOP_HEADERS and not (
                    rewrite_tool_name and name.lower() == "content-length"
                ):
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()

            def response_chunks() -> Iterator[bytes]:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        return
                    yield chunk

            chunks = response_chunks()
            if rewrite_tool_name:
                chunks = rewrite_direct_vision_tool_calls(chunks)
            for chunk in chunks:
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
    observation_cache_path: Path | None = None,
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
    server.vision_broker = VisionBroker(
        api_key=gemini_api_key,
        model=gemini_model,
        cache_path=observation_cache_path
        or (Path.cwd() / f".apicodex-vision-{profile_id}.json"),
    )
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
