from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import codex_vision_proxy


PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


class _JsonResponse:
    def __init__(self, payload: dict[str, object], status: int = 200) -> None:
        self.status = status
        self.headers = {"Content-Type": "application/json"}
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class CodexVisionProxyTests(unittest.TestCase):
    def test_gemini_interaction_uses_header_auth_and_non_stored_image_input(self) -> None:
        captured: list[Request] = []

        def fake_open(request: Request, timeout: float):
            captured.append(request)
            self.assertEqual(timeout, 30)
            return _JsonResponse(
                {
                    "status": "completed",
                    "steps": [
                        {
                            "type": "model_output",
                            "content": [
                                {"type": "text", "text": "A blue login dialog."}
                            ],
                        }
                    ],
                }
            )

        result = codex_vision_proxy.request_gemini_vision(
            api_key="gemini-secret",
            model="gemini-3.5-flash-lite",
            user_prompt="Explain the error in this screenshot.",
            images=[codex_vision_proxy.VisionImage("image/png", PNG_DATA_URL)],
            open_request=fake_open,
        )

        self.assertEqual(result, "A blue login dialog.")
        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/interactions",
        )
        self.assertNotIn("gemini-secret", request.full_url)
        self.assertEqual(request.get_header("X-goog-api-key"), "gemini-secret")
        payload = json.loads(request.data)
        self.assertEqual(payload["model"], "gemini-3.5-flash-lite")
        self.assertFalse(payload["store"])
        self.assertEqual(payload["generation_config"]["thinking_level"], "minimal")
        self.assertEqual(payload["response_format"], {"type": "text"})
        image = next(item for item in payload["input"] if item["type"] == "image")
        self.assertEqual(image["mime_type"], "image/png")
        self.assertNotIn("data:image/png;base64,", image["data"])

    def test_enrichment_replaces_images_without_mutating_original_request(self) -> None:
        payload = {
            "model": "text-only-model",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What does this UI show?"},
                        {"type": "input_image", "image_url": PNG_DATA_URL},
                    ],
                }
            ],
            "stream": True,
        }
        calls: list[tuple[str, list[codex_vision_proxy.VisionImage]]] = []

        def analyze(prompt: str, images: list[codex_vision_proxy.VisionImage]) -> str:
            calls.append((prompt, images))
            return "The screenshot shows a disabled Save button."

        enriched, changed = codex_vision_proxy.enrich_responses_request(
            payload,
            analyze=analyze,
        )

        self.assertTrue(changed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "What does this UI show?")
        self.assertEqual(calls[0][1][0].mime_type, "image/png")
        original_content = payload["input"][0]["content"]
        self.assertEqual(original_content[1]["type"], "input_image")
        content = enriched["input"][0]["content"]
        self.assertFalse(any(item.get("type") == "input_image" for item in content))
        self.assertIn("disabled Save button", content[1]["text"])
        self.assertTrue(enriched["stream"])

    def test_enrichment_is_a_noop_for_text_only_requests(self) -> None:
        payload = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                }
            ]
        }

        enriched, changed = codex_vision_proxy.enrich_responses_request(
            payload,
            analyze=lambda *_args: self.fail("vision model called for text"),
        )

        self.assertFalse(changed)
        self.assertIs(enriched, payload)

    def test_on_demand_text_request_adds_final_status_instruction(self) -> None:
        payload = {
            "instructions": "Keep answers concise.",
            "input": [{"role": "user", "content": "What is 1 + 1?"}],
        }

        enriched, changed = codex_vision_proxy.enrich_responses_request(
            payload,
            analyze=lambda *_args: self.fail("Gemini called for a text request"),
            on_demand=True,
        )

        self.assertTrue(changed)
        self.assertIsNot(enriched, payload)
        self.assertEqual(payload["instructions"], "Keep answers concise.")
        self.assertIn("Keep answers concise.", enriched["instructions"])
        self.assertIn("视觉辅助：本轮未调用 Gemini", enriched["instructions"])
        self.assertIn(
            "ApiCodex mandatory response footer",
            enriched["input"][0]["content"],
        )
        self.assertEqual(payload["input"][0]["content"], "What is 1 + 1?")

    def test_on_demand_enrichment_registers_images_without_calling_gemini(self) -> None:
        payload = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Compare these images."},
                        {"type": "input_image", "image_url": PNG_DATA_URL},
                    ],
                }
            ]
        }
        registered: list[list[codex_vision_proxy.VisionImage]] = []

        enriched, changed = codex_vision_proxy.enrich_responses_request(
            payload,
            analyze=lambda *_args: self.fail("Gemini must be model-triggered"),
            on_demand=True,
            register=lambda images: registered.append(images) or ["sha256:test-image"],
        )

        self.assertTrue(changed)
        self.assertEqual(len(registered), 1)
        content = enriched["input"][0]["content"]
        self.assertFalse(any(item.get("type") == "input_image" for item in content))
        self.assertIn("sha256:test-image", content[1]["text"])
        self.assertIn("mcp__apicodex_vision__inspect_images", content[1]["text"])
        self.assertNotIn("sha256:test-image", json.dumps(payload))

    def test_direct_tool_call_rewrite_handles_every_chunk_boundary(self) -> None:
        event = (
            b'data: {"type":"response.output_item.done","item":'
            b'{"type":"function_call","name":'
            b'"mcp__apicodex_visioninspect_images","arguments":"{}",'
            b'"call_id":"call-1"}}\r\n\r\n'
        )
        for split in range(len(event) + 1):
            with self.subTest(split=split):
                actual = b"".join(
                    codex_vision_proxy.rewrite_direct_vision_tool_calls(
                        [event[:split], event[split:]]
                    )
                )
                payload = json.loads(actual.splitlines()[0][6:])
                item = payload["item"]
                self.assertEqual(item["name"], "inspect_images")
                self.assertEqual(item["namespace"], "mcp__apicodex_vision")

        untouched = b'{"name":"mcp__other__inspect_images"}'
        self.assertEqual(
            b"".join(
                codex_vision_proxy.rewrite_direct_vision_tool_calls([untouched])
            ),
            untouched,
        )

    def test_broker_reuses_persistent_observation_for_same_images_and_focus(self) -> None:
        calls: list[tuple[str, list[codex_vision_proxy.VisionImage]]] = []

        def analyze(prompt: str, images: list[codex_vision_proxy.VisionImage]) -> str:
            calls.append((prompt, images))
            return "Two sketches with different color palettes."

        with tempfile.TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "vision-observations.json"
            broker = codex_vision_proxy.VisionBroker(
                api_key="gemini-secret",
                model="gemini-3.5-flash-lite",
                cache_path=cache_path,
                analyze=analyze,
            )
            image_ids = broker.register_images(
                [codex_vision_proxy.VisionImage("image/png", PNG_DATA_URL)]
            )
            first = broker.inspect(image_ids, "Compare composition and color.")
            second = broker.inspect(image_ids, "  compare composition and color.  ")

            reloaded = codex_vision_proxy.VisionBroker(
                api_key="gemini-secret",
                model="gemini-3.5-flash-lite",
                cache_path=cache_path,
                analyze=lambda *_args: self.fail("persistent cache was not reused"),
            )
            third = reloaded.inspect(image_ids, "Compare composition and color.")

        self.assertEqual(len(calls), 1)
        self.assertTrue(first.gemini_invoked)
        self.assertFalse(first.cache_hit)
        self.assertFalse(second.gemini_invoked)
        self.assertTrue(second.cache_hit)
        self.assertFalse(third.gemini_invoked)
        self.assertTrue(third.cache_hit)
        self.assertEqual(third.description, first.description)

    def test_broker_calls_gemini_again_for_a_new_visual_focus(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmp:
            broker = codex_vision_proxy.VisionBroker(
                api_key="gemini-secret",
                model="gemini-3.5-flash-lite",
                cache_path=Path(tmp) / "vision-observations.json",
                analyze=lambda prompt, _images: calls.append(prompt) or prompt,
            )
            image_ids = broker.register_images(
                [codex_vision_proxy.VisionImage("image/png", PNG_DATA_URL)]
            )
            broker.inspect(image_ids, "Describe the composition.")
            broker.inspect(image_ids, "Read the small text in the corner.")

        self.assertEqual(len(calls), 2)

    def test_loopback_proxy_enriches_responses_and_preserves_upstream_auth(self) -> None:
        received: list[dict[str, object]] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                received.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "payload": json.loads(self.rfile.read(size)),
                    }
                )
                body = b'{"id":"response-test","status":"completed"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return None

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        upstream_url = f"http://127.0.0.1:{upstream.server_port}/v1"
        request_payload = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Inspect this"},
                        {"type": "input_image", "image_url": PNG_DATA_URL},
                    ],
                }
            ]
        }
        try:
            with (
                patch.object(
                    codex_vision_proxy,
                    "request_gemini_vision",
                    return_value="A terminal showing HTTP 403.",
                ),
                codex_vision_proxy.vision_proxy(
                    upstream_base_url=upstream_url,
                    upstream_api_key="upstream-secret",
                    gemini_api_key="gemini-secret",
                    gemini_model="gemini-3.5-flash-lite",
                    control_token="control-secret",
                ) as endpoint,
            ):
                request = Request(
                    endpoint.base_url + "/responses",
                    data=json.dumps(request_payload).encode("utf-8"),
                    headers={
                        "Authorization": "Bearer upstream-secret",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("response-test", response.read().decode("utf-8"))

            self.assertEqual(len(received), 1)
            self.assertEqual(received[0]["path"], "/v1/responses")
            self.assertEqual(received[0]["authorization"], "Bearer upstream-secret")
            content = received[0]["payload"]["input"][0]["content"]
            self.assertFalse(any(item.get("type") == "input_image" for item in content))
            self.assertIn("HTTP 403", content[1]["text"])
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    def test_on_demand_proxy_calls_gemini_only_from_inspection_endpoint(self) -> None:
        received: list[dict[str, object]] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                received.append(json.loads(self.rfile.read(size)))
                body = b'{"id":"response-lazy","status":"completed"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:
                return None

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        request_payload = {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What do you think?"},
                        {"type": "input_image", "image_url": PNG_DATA_URL},
                    ],
                }
            ]
        }
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with (
                    patch.object(
                        codex_vision_proxy,
                        "request_gemini_vision",
                        return_value="A small monochrome drawing.",
                    ) as gemini,
                    codex_vision_proxy.vision_proxy(
                        upstream_base_url=(
                            f"http://127.0.0.1:{upstream.server_port}/v1"
                        ),
                        upstream_api_key="upstream-secret",
                        gemini_api_key="gemini-secret",
                        gemini_model="gemini-3.5-flash-lite",
                        control_token="control-secret",
                        observation_cache_path=Path(tmp) / "observations.json",
                    ) as endpoint,
                ):
                    request = Request(
                        endpoint.base_url.replace(
                            "/v1", "/__apicodex_vision__/on-demand/v1"
                        )
                        + "/responses",
                        data=json.dumps(request_payload).encode("utf-8"),
                        headers={
                            "Authorization": "Bearer upstream-secret",
                            "Content-Type": "application/json",
                        },
                        method="POST",
                    )
                    with urlopen(request, timeout=5) as response:
                        self.assertEqual(response.status, 200)
                    gemini.assert_not_called()

                    placeholder = received[0]["input"][0]["content"][1]["text"]
                    image_id = placeholder.split("tool: ", 1)[1].split(".", 1)[0]
                    inspect_body = json.dumps(
                        {
                            "image_ids": [image_id],
                            "focus": "Describe the drawing.",
                        }
                    ).encode("utf-8")
                    for _ in range(2):
                        inspect_request = Request(
                            f"http://127.0.0.1:{endpoint.port}"
                            + codex_vision_proxy.INSPECT_PATH,
                            data=inspect_body,
                            headers={
                                "Content-Type": "application/json",
                                "X-ApiCodex-Vision-Control": "control-secret",
                            },
                            method="POST",
                        )
                        with urlopen(inspect_request, timeout=5) as response:
                            result = json.loads(response.read())

                    self.assertEqual(gemini.call_count, 1)
                    self.assertFalse(result["geminiInvoked"])
                    self.assertTrue(result["cacheHit"])
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)

    def test_loopback_proxy_rejects_wrong_bearer_without_contacting_upstream(self) -> None:
        requests_received: list[str] = []

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests_received.append(self.path)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *_args) -> None:
                return None

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with codex_vision_proxy.vision_proxy(
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                upstream_api_key="upstream-secret",
                gemini_api_key="gemini-secret",
                gemini_model="gemini-3.5-flash-lite",
                control_token="control-secret",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/models",
                    headers={"Authorization": "Bearer wrong-secret"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)

            self.assertEqual(raised.exception.code, 401)
            self.assertEqual(requests_received, [])
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
