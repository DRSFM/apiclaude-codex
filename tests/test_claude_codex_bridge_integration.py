from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
import unittest
import urllib.error
import urllib.request
import warnings
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_codex_bridge import cpa_bridge, litellm_bridge


@unittest.skipUnless(
    os.environ.get("APICLAUDE_LITELLM_INTEGRATION") == "1",
    "set APICLAUDE_LITELLM_INTEGRATION=1 to run the LiteLLM integration test",
)
class ClaudeCodexBridgeIntegrationTests(unittest.TestCase):
    def test_anthropic_tool_request_round_trips_through_responses_api(self) -> None:
        captured: dict[str, object] = {}
        serializer_warnings: list[str] = []
        serializer_warning_seen = threading.Event()
        original_showwarning = warnings.showwarning

        def capture_serializer_warning(
            message: Warning,
            category: type[Warning],
            filename: str,
            lineno: int,
            file: object | None = None,
            line: str | None = None,
        ) -> None:
            text = str(message)
            if "Expected `ResponseAPIUsage`" in text:
                serializer_warnings.append(text)
                serializer_warning_seen.set()
                return
            original_showwarning(message, category, filename, lineno, file, line)

        warnings.showwarning = capture_serializer_warning
        self.addCleanup(setattr, warnings, "showwarning", original_showwarning)
        captured_stderr = io.StringIO()
        original_stderr = sys.stderr
        sys.stderr = captured_stderr
        self.addCleanup(setattr, sys, "stderr", original_stderr)
        asyncio_errors: list[str] = []

        class CaptureAsyncioErrors(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                asyncio_errors.append(record.getMessage())

        asyncio_handler = CaptureAsyncioErrors(level=logging.ERROR)
        asyncio_logger = logging.getLogger("asyncio")
        asyncio_logger.addHandler(asyncio_handler)
        self.addCleanup(asyncio_logger.removeHandler, asyncio_handler)

        class FakeResponsesHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("Authorization")
                captured["user_agent"] = self.headers.get("User-Agent")
                captured["originator"] = self.headers.get("originator")
                captured["body"] = json.loads(self.rfile.read(length))
                events = [
                    {
                        "type": "response.created",
                        "sequence_number": 0,
                        "response": {
                            "id": "resp_test",
                            "object": "response",
                            "created_at": 1,
                            "status": "in_progress",
                            "model": "gpt-5.6-sol",
                            "output": [],
                            "parallel_tool_calls": True,
                            "tool_choice": "auto",
                            "tools": [],
                            "usage": None,
                        },
                    },
                    {
                        "type": "response.output_item.added",
                        "sequence_number": 1,
                        "response_id": "resp_test",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_test",
                            "call_id": "call_test",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "sequence_number": 2,
                        "response_id": "resp_test",
                        "item_id": "fc_test",
                        "output_index": 0,
                        "delta": '{"path":"README.md"}',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "sequence_number": 3,
                        "response_id": "resp_test",
                        "item_id": "fc_test",
                        "output_index": 0,
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                    {
                        "type": "response.output_item.done",
                        "sequence_number": 4,
                        "response_id": "resp_test",
                        "output_index": 0,
                        "item": {
                            "type": "function_call",
                            "id": "fc_test",
                            "call_id": "call_test",
                            "name": "read_file",
                            "arguments": '{"path":"README.md"}',
                            "status": "completed",
                        },
                    },
                    {
                        "type": "response.completed",
                        "sequence_number": 5,
                        "response": {
                            "id": "resp_test",
                            "object": "response",
                            "created_at": 1,
                            "status": "completed",
                            "model": "gpt-5.6-sol",
                            "output": [
                                {
                                    "type": "function_call",
                                    "id": "fc_test",
                                    "call_id": "call_test",
                                    "name": "read_file",
                                    "arguments": '{"path":"README.md"}',
                                    "status": "completed",
                                }
                            ],
                            "parallel_tool_calls": True,
                            "tool_choice": "auto",
                            "tools": [],
                            "usage": {
                                "input_tokens": 12,
                                "input_tokens_details": {"cached_tokens": 0},
                                "output_tokens": 8,
                                "total_tokens": 20,
                                "output_tokens_details": {"reasoning_tokens": 0},
                            },
                        },
                    },
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    payload = json.dumps(event, separators=(",", ":"))
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {payload}\n\n".encode()
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                self.close_connection = True

            def log_message(self, _format: str, *args: object) -> None:
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}/v1"
            with litellm_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="sk-test-upstream",
                model="gpt-5.6-sol",
            ) as endpoint:
                request_body = {
                    "model": "gpt-5.6-sol",
                    "max_tokens": 128,
                    "stream": True,
                    "messages": [{"role": "user", "content": "Read README.md"}],
                    "tools": [
                        {
                            "name": "read_file",
                            "description": "Read one file",
                            "input_schema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                                "required": ["path"],
                            },
                        }
                    ],
                }
                request = urllib.request.Request(
                    f"{endpoint.base_url}/v1/messages",
                    data=json.dumps(request_body).encode(),
                    headers={
                        "Authorization": f"Bearer {endpoint.token}",
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    method="POST",
                )
                try:
                    anthropic_streams = []
                    for _ in range(2):
                        with urllib.request.urlopen(request, timeout=30) as response:
                            anthropic_streams.append(response.read().decode())
                    anthropic_stream = anthropic_streams[-1]
                except urllib.error.HTTPError as exc:
                    self.fail(
                        f"bridge returned HTTP {exc.code}: "
                        f"{exc.read().decode(errors='replace')}; "
                        f"captured={captured!r}"
                    )
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)

        serializer_warning_seen.wait(timeout=1)
        self.assertIn(captured["path"], ("/v1/responses", "/responses"))
        self.assertEqual(captured["authorization"], "Bearer sk-test-upstream")
        self.assertEqual(
            captured["user_agent"],
            "codex_cli_rs/apiclaude-bridge",
        )
        self.assertEqual(captured["originator"], "apiclaude_codex_bridge")
        upstream_body = captured["body"]
        self.assertIsInstance(upstream_body, dict)
        self.assertEqual(upstream_body["model"], "gpt-5.6-sol")
        self.assertEqual(upstream_body["tools"][0]["name"], "read_file")
        self.assertIn("content_block_start", anthropic_stream)
        self.assertIn('"type": "tool_use"', anthropic_stream)
        self.assertIn("input_json_delta", anthropic_stream)
        self.assertIn("message_stop", anthropic_stream)
        self.assertEqual(serializer_warnings, [])
        self.assertNotIn(
            "Task exception was never retrieved",
            captured_stderr.getvalue(),
        )
        self.assertFalse(
            any(
                "Task exception was never retrieved" in message
                for message in asyncio_errors
            ),
            asyncio_errors,
        )


@unittest.skipUnless(
    os.environ.get("APICLAUDE_CPA_INTEGRATION") == "1"
    and Path(os.environ.get("APICLAUDE_CPA_EXE", "")).is_file(),
    "set APICLAUDE_CPA_INTEGRATION=1 and APICLAUDE_CPA_EXE to run",
)
class ClaudeCodexCpaIntegrationTests(unittest.TestCase):
    def test_anthropic_request_round_trips_through_cpa_responses(self) -> None:
        captured: dict[str, object] = {}

        class FakeResponsesHandler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                captured["path"] = self.path
                captured["authorization"] = self.headers.get("Authorization")
                captured["body"] = json.loads(self.rfile.read(length))
                response = {
                    "id": "resp_cpa_test",
                    "object": "response",
                    "created_at": 1,
                    "status": "completed",
                    "model": "gpt-5.6-sol",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_cpa_test",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "CPA bridge works",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 3,
                        "total_tokens": 8,
                    },
                }
                events = [
                    {
                        "type": "response.created",
                        "sequence_number": 0,
                        "response": {**response, "status": "in_progress", "output": []},
                    },
                    {
                        "type": "response.output_item.added",
                        "sequence_number": 1,
                        "output_index": 0,
                        "item": {
                            "type": "message",
                            "id": "msg_cpa_test",
                            "status": "in_progress",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                    {
                        "type": "response.content_part.added",
                        "sequence_number": 2,
                        "item_id": "msg_cpa_test",
                        "output_index": 0,
                        "content_index": 0,
                        "part": {
                            "type": "output_text",
                            "text": "",
                            "annotations": [],
                        },
                    },
                    {
                        "type": "response.output_text.delta",
                        "sequence_number": 3,
                        "item_id": "msg_cpa_test",
                        "output_index": 0,
                        "content_index": 0,
                        "delta": "CPA bridge works",
                    },
                    {
                        "type": "response.output_text.done",
                        "sequence_number": 4,
                        "item_id": "msg_cpa_test",
                        "output_index": 0,
                        "content_index": 0,
                        "text": "CPA bridge works",
                    },
                    {
                        "type": "response.output_item.done",
                        "sequence_number": 5,
                        "output_index": 0,
                        "item": response["output"][0],
                    },
                    {
                        "type": "response.completed",
                        "sequence_number": 6,
                        "response": response,
                    },
                ]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    payload = json.dumps(event, separators=(",", ":"))
                    self.wfile.write(
                        f"event: {event['type']}\ndata: {payload}\n\n".encode()
                    )
                    self.wfile.flush()
                self.wfile.write(b"data: [DONE]\n\n")
                self.close_connection = True

            def log_message(self, _format: str, *args: object) -> None:
                return

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), FakeResponsesHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()
        try:
            with cpa_bridge(
                upstream_base_url=f"http://127.0.0.1:{upstream.server_port}/v1",
                upstream_api_key="sk-test-upstream",
                model="gpt-5.6-sol",
                cpa_executable=os.environ["APICLAUDE_CPA_EXE"],
            ) as endpoint:
                request = urllib.request.Request(
                    f"{endpoint.base_url}/v1/messages",
                    data=json.dumps(
                        {
                            "model": "gpt-5.6-sol",
                            "max_tokens": 128,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers={
                        "Authorization": f"Bearer {endpoint.token}",
                        "Content-Type": "application/json",
                        "anthropic-version": "2023-06-01",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=30) as response:
                    anthropic_stream = response.read().decode()
        finally:
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)

        self.assertEqual(captured["path"], "/v1/responses")
        self.assertEqual(captured["authorization"], "Bearer sk-test-upstream")
        self.assertEqual(captured["body"]["model"], "gpt-5.6-sol")
        self.assertIn("CPA bridge works", anthropic_stream)
        self.assertIn("message_stop", anthropic_stream)


if __name__ == "__main__":
    unittest.main()
