"""Minimal STDIO MCP server for model-controlled ApiCodex vision inspection."""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, TextIO
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from codex_vision_proxy import (
    INSPECT_PATH,
    MAX_GEMINI_OUTPUT_BYTES,
    MAX_VISION_PROMPT_CHARS,
    VisionInspection,
    VisionProxyError,
)


MCP_INSTRUCTIONS = (
    "Use mcp__apicodex_vision__inspect_images only when the user's request needs "
    "visual evidence and the visual observations already in the conversation "
    "are insufficient, or when the user explicitly requests a fresh inspection. "
    "Do not call merely because image placeholders occur in replayed history. In every final "
    "user-facing answer append exactly one status line: '视觉辅助：本轮已调用 Gemini' "
    "when the tool reports gemini_invoked=true; '视觉辅助：本轮未调用 Gemini（复用缓存）' "
    "when it reports cache_hit=true; otherwise '视觉辅助：本轮未调用 Gemini'. Never "
    "claim a Gemini call without tool-result evidence."
)


def request_worker_inspection(
    endpoint_origin: str,
    control_token: str,
    image_ids: list[str],
    focus: str,
    refresh: bool,
) -> VisionInspection:
    body = json.dumps(
        {"image_ids": image_ids, "focus": focus, "refresh": refresh},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    request = Request(
        endpoint_origin.rstrip("/") + INSPECT_PATH,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-ApiCodex-Vision-Control": control_token,
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=45) as response:
            raw = response.read(MAX_GEMINI_OUTPUT_BYTES + 1)
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read(64 * 1024).decode("utf-8-sig"))
            detail = payload.get("error", {}).get("message")
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            detail = None
        raise VisionProxyError(str(detail or f"vision worker HTTP {exc.code}")) from exc
    except (URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise VisionProxyError(f"vision worker request failed: {reason}") from exc
    if len(raw) > MAX_GEMINI_OUTPUT_BYTES:
        raise VisionProxyError("vision worker response exceeded the local size limit")
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
        image_values = payload["imageIds"]
        description = payload["description"]
        gemini_invoked = payload["geminiInvoked"]
        cache_hit = payload["cacheHit"]
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise VisionProxyError("vision worker returned an invalid response") from exc
    if (
        not isinstance(image_values, list)
        or not all(isinstance(value, str) for value in image_values)
        or not isinstance(description, str)
        or not isinstance(gemini_invoked, bool)
        or not isinstance(cache_hit, bool)
    ):
        raise VisionProxyError("vision worker returned invalid result fields")
    return VisionInspection(
        image_ids=tuple(image_values),
        description=description,
        gemini_invoked=gemini_invoked,
        cache_hit=cache_hit,
    )


class VisionMcpServer:
    def __init__(
        self,
        *,
        inspect: Callable[[list[str], str, bool], VisionInspection],
    ) -> None:
        self.inspect = inspect

    @staticmethod
    def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None:
            return None
        if method == "initialize":
            params = message.get("params")
            requested_version = (
                params.get("protocolVersion") if isinstance(params, dict) else None
            )
            protocol_version = (
                requested_version
                if isinstance(requested_version, str)
                else "2025-06-18"
            )
            return self._result(
                request_id,
                {
                    "protocolVersion": protocol_version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "apicodex-vision",
                        "version": "1.0.0",
                    },
                    "instructions": MCP_INSTRUCTIONS,
                },
            )
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(
                request_id,
                {
                    "tools": [
                        {
                            "name": "inspect_images",
                            "title": "Inspect attached images with Gemini",
                            "description": (
                                "Ask the configured Gemini visual model a focused "
                                "question about registered ApiCodex image IDs. Use "
                                "only when existing observations are insufficient. "
                                "Identical image IDs and focus reuse local cache; set "
                                "refresh only when the user explicitly requests a "
                                "fresh inspection."
                            ),
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "image_ids": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 1,
                                        "description": (
                                            "Exact sha256 image IDs shown in the "
                                            "conversation placeholders."
                                        ),
                                    },
                                    "focus": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": MAX_VISION_PROMPT_CHARS,
                                        "description": (
                                            "The specific visual evidence needed to "
                                            "answer the current user request."
                                        ),
                                    },
                                    "refresh": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": (
                                            "Bypass a matching observation cache only "
                                            "after an explicit user request."
                                        ),
                                    },
                                },
                                "required": ["image_ids", "focus"],
                                "additionalProperties": False,
                            },
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "idempotentHint": True,
                                "openWorldHint": False,
                            },
                        }
                    ]
                },
            )
        if method != "tools/call":
            return self._error(request_id, -32601, "Method not found")
        params = message.get("params")
        if not isinstance(params, dict) or params.get("name") != "inspect_images":
            return self._error(request_id, -32602, "Unknown vision tool")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            return self._error(request_id, -32602, "Tool arguments must be an object")
        image_ids = arguments.get("image_ids")
        focus = arguments.get("focus")
        refresh = arguments.get("refresh", False)
        if (
            not isinstance(image_ids, list)
            or not image_ids
            or not all(isinstance(value, str) for value in image_ids)
            or not isinstance(focus, str)
            or not focus.strip()
            or not isinstance(refresh, bool)
        ):
            return self._error(request_id, -32602, "Invalid vision tool arguments")
        try:
            inspection = self.inspect(image_ids, focus, refresh)
        except VisionProxyError as exc:
            return self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            )
        text = (
            "[ApiCodex visual observation]\n"
            f"gemini_invoked={str(inspection.gemini_invoked).lower()}\n"
            f"cache_hit={str(inspection.cache_hit).lower()}\n"
            f"image_ids={json.dumps(list(inspection.image_ids), ensure_ascii=False)}\n"
            f"{inspection.description}\n"
            "[End ApiCodex visual observation]"
        )
        return self._result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": False},
        )


def serve_stdio(
    server: VisionMcpServer,
    *,
    input_stream: TextIO | None = None,
    output_stream: TextIO | None = None,
) -> int:
    source = input_stream or sys.stdin
    target = output_stream or sys.stdout
    for line in source:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("MCP message must be an object")
            response = server.handle(message)
        except (json.JSONDecodeError, ValueError) as exc:
            response = VisionMcpServer._error(None, -32700, str(exc))
        if response is not None:
            target.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
            target.write("\n")
            target.flush()
    return 0


def run_vision_mcp(endpoint_origin: str, control_token: str) -> int:
    server = VisionMcpServer(
        inspect=lambda image_ids, focus, refresh: request_worker_inspection(
            endpoint_origin,
            control_token,
            image_ids,
            focus,
            refresh,
        )
    )
    return serve_stdio(server)
