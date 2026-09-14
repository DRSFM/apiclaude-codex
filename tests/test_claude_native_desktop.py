from __future__ import annotations

import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import apiagent
import claude_codex_bridge
from secure_store import SecureStore
from tests.support import KeychainIsolationMixin


class _NativeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        super().__init__(("127.0.0.1", 0), handler)
        self.requests: list[dict[str, object]] = []
        self.mode = "normal"
        self.release_stream = threading.Event()


class _NativeUpstreamHandler(BaseHTTPRequestHandler):
    server: _NativeUpstream

    def log_message(self, _format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.server.requests.append(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("x-api-key"),
                "anthropic_beta": self.headers.get("anthropic-beta"),
            }
        )
        if self.server.mode == "unauthorized":
            self.send_error(401)
            return
        if self.server.mode == "leaky-error":
            raw = b'{"error":"upstream-secret was rejected"}'
            self.send_response(429)
            self.send_header("Content-Type", "application/json")
            self.send_header("X-Upstream-Detail", "upstream-secret rejected")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        if self.server.mode == "invalid-json":
            raw = b"not json"
        elif self.server.mode == "no-claude":
            raw = b'{"data":[{"id":"glm-5.2"}],"has_more":false}'
        elif "after_id=page-1" in self.path:
            raw = json.dumps(
                {
                    "data": [
                        {"id": "claude-opus-5"},
                        {"id": "claude-sonnet-5"},
                    ],
                    "has_more": False,
                }
            ).encode()
        else:
            raw = json.dumps(
                {
                    "data": [
                        {"id": "glm-5.2"},
                        {"id": "claude-sonnet-5"},
                    ],
                    "has_more": True,
                    "last_id": "page-1",
                }
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.server.requests.append(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "api_key": self.headers.get("x-api-key"),
                "anthropic_version": self.headers.get("anthropic-version"),
                "anthropic_beta": self.headers.get("anthropic-beta"),
                "body": body,
            }
        )
        if self.server.mode == "slow-sse":
            first = b"event: content_block_delta\ndata: {\"delta\":\"first\"}\n\n"
            second = b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(first)
            self.wfile.flush()
            self.server.release_stream.wait(timeout=2)
            self.wfile.write(second)
            self.wfile.flush()
            self.close_connection = True
            return
        raw = b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@contextmanager
def _native_upstream():
    server = _NativeUpstream(_NativeUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class NativeClaudeGatewayTests(unittest.TestCase):
    def test_upstream_url_accepts_root_and_v1_base_urls(self) -> None:
        self.assertEqual(
            claude_codex_bridge._anthropic_upstream_url(
                "https://relay.test", "/v1/messages"
            ),
            "https://relay.test/v1/messages",
        )
        self.assertEqual(
            claude_codex_bridge._anthropic_upstream_url(
                "https://relay.test/v1", "/v1/messages/count_tokens"
            ),
            "https://relay.test/v1/messages/count_tokens",
        )

    def test_passthrough_preserves_body_stream_and_replaces_auth(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            body = b'{"model":"claude-sonnet-5","stream":true}'
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/messages",
                    data=body,
                    headers={
                        "Authorization": "Bearer local-token",
                        "anthropic-version": "2023-06-01",
                    },
                )
                with urlopen(request, timeout=5) as response:
                    result = response.read()

        self.assertEqual(
            result,
            b"event: message_stop\ndata: {\"type\":\"message_stop\"}\n\n",
        )
        received = upstream.requests[0]
        self.assertEqual(received["path"], "/v1/messages")
        self.assertEqual(received["body"], body)
        self.assertEqual(received["authorization"], "Bearer upstream-secret")
        self.assertEqual(received["api_key"], "upstream-secret")
        self.assertEqual(received["anthropic_version"], "2023-06-01")
        self.assertIsNone(received["anthropic_beta"])
        self.assertNotIn("local-token", str(received))

    def test_1m_mode_normalizes_model_and_merges_beta_header(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            body = b'{ "model": "claude-opus-5[1m]", "messages": [] }'
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
                enable_1m=True,
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/messages",
                    data=body,
                    headers={
                        "x-api-key": "local-token",
                        "anthropic-beta": "prompt-caching-2024-07-31",
                    },
                )
                with urlopen(request, timeout=5) as response:
                    response.read()

        received = upstream.requests[0]
        self.assertEqual(json.loads(received["body"])["model"], "claude-opus-5")
        beta = str(received["anthropic_beta"])
        self.assertIn("prompt-caching-2024-07-31", beta)
        self.assertIn("context-1m-2025-08-07", beta)
        self.assertEqual(beta.count("context-1m-2025-08-07"), 1)

    def test_1m_mode_preserves_non_json_body_and_adds_beta(self) -> None:
        headers = {"Content-Type": "application/octet-stream"}
        body = b"not-json\x00body"

        normalized = claude_codex_bridge._prepare_anthropic_1m_request(
            body,
            headers,
            path="/v1/messages/count_tokens",
            enabled=True,
        )

        self.assertIs(normalized, body)
        self.assertEqual(
            headers["anthropic-beta"],
            "context-1m-2025-08-07",
        )

    def test_sse_first_event_is_forwarded_before_upstream_finishes(self) -> None:
        with _native_upstream() as upstream:
            upstream.mode = "slow-sse"
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/messages",
                    data=b'{"model":"claude-opus-5","stream":true}',
                    headers={"x-api-key": "local-token"},
                )
                started = time.monotonic()
                with urlopen(request, timeout=5) as response:
                    first_line = response.readline()
                    elapsed = time.monotonic() - started
                    upstream.release_stream.set()
                    remainder = response.read()

        self.assertEqual(first_line, b"event: content_block_delta\n")
        self.assertLess(elapsed, 1.0)
        self.assertIn(b"event: message_stop", remainder)

    def test_passthrough_rejects_wrong_local_token(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/models",
                    headers={"x-api-key": "wrong-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 401)
                raised.exception.close()
        self.assertEqual(upstream.requests, [])

    def test_count_tokens_path_is_forwarded_without_conversion(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            body = b'{"model":"claude-sonnet-5","messages":[]}'
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/messages/count_tokens",
                    data=body,
                    headers={"x-api-key": "local-token"},
                )
                with urlopen(request, timeout=5) as response:
                    response.read()
        self.assertEqual(upstream.requests[0]["path"], "/v1/messages/count_tokens")
        self.assertEqual(upstream.requests[0]["body"], body)

    def test_model_detail_get_is_forwarded_but_post_is_rejected(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                detail_url = endpoint.base_url + "/v1/models/claude-opus-5"
                with urlopen(
                    Request(detail_url, headers={"x-api-key": "local-token"}),
                    timeout=5,
                ) as response:
                    response.read()
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        Request(
                            detail_url,
                            data=b"{}",
                            headers={"x-api-key": "local-token"},
                        ),
                        timeout=5,
                    )
                raised.exception.close()

        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(upstream.requests[0]["path"], "/v1/models/claude-opus-5")
        self.assertEqual(len(upstream.requests), 1)

    def test_upstream_connection_error_redacts_secret(self) -> None:
        opener = Mock()
        opener.open.side_effect = URLError("upstream-secret failed")
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint, patch.object(
                claude_codex_bridge,
                "_proxy_opener",
                return_value=opener,
            ):
                request = Request(
                    endpoint.base_url + "/v1/models",
                    headers={"x-api-key": "local-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                body = raised.exception.read().decode()
                raised.exception.close()
        self.assertEqual(raised.exception.code, 502)
        self.assertNotIn("upstream-secret", body)
        self.assertIn("<redacted>", body)

    def test_upstream_http_error_redacts_secret_and_preserves_status(self) -> None:
        with _native_upstream() as upstream:
            upstream.mode = "leaky-error"
            upstream_url = f"http://127.0.0.1:{upstream.server_port}"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                request = Request(
                    endpoint.base_url + "/v1/models",
                    headers={"x-api-key": "local-token"},
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                body = raised.exception.read().decode()
                detail = raised.exception.headers["X-Upstream-Detail"]
                raised.exception.close()

        self.assertEqual(raised.exception.code, 429)
        self.assertNotIn("upstream-secret", body)
        self.assertNotIn("upstream-secret", detail)
        self.assertIn("<redacted>", body)

    def test_model_discovery_is_paginated_filtered_and_deduplicated(self) -> None:
        with _native_upstream() as upstream:
            upstream_url = f"http://127.0.0.1:{upstream.server_port}/v1"
            with claude_codex_bridge.anthropic_passthrough_bridge(
                upstream_base_url=upstream_url,
                upstream_api_key="upstream-secret",
                local_token="local-token",
                proxy_url="direct",
            ) as endpoint:
                models = claude_codex_bridge.discover_anthropic_models(
                    gateway_base_url=endpoint.base_url,
                    local_token="local-token",
                )

        self.assertEqual(models, ["claude-sonnet-5", "claude-opus-5"])
        self.assertEqual(len(upstream.requests), 2)
        self.assertIn("after_id=page-1", str(upstream.requests[1]["path"]))

    def test_model_discovery_reports_http_and_invalid_json(self) -> None:
        for mode, expected in (
            ("unauthorized", "HTTP 401"),
            ("invalid-json", "invalid JSON"),
            ("no-claude", "found no claude-\\* models"),
        ):
            with self.subTest(mode=mode), _native_upstream() as upstream:
                upstream.mode = mode
                upstream_url = f"http://127.0.0.1:{upstream.server_port}"
                with claude_codex_bridge.anthropic_passthrough_bridge(
                    upstream_base_url=upstream_url,
                    upstream_api_key="upstream-secret",
                    local_token="local-token",
                    proxy_url="direct",
                ) as endpoint, self.assertRaisesRegex(
                    claude_codex_bridge.BridgeStartupError,
                    expected,
                ):
                    claude_codex_bridge.discover_anthropic_models(
                        gateway_base_url=endpoint.base_url,
                        local_token="local-token",
                    )

    def test_proxy_opener_uses_configured_proxy_or_explicit_direct_mode(self) -> None:
        with (
            patch.object(claude_codex_bridge.urllib_request, "ProxyHandler") as handler,
            patch.object(claude_codex_bridge.urllib_request, "build_opener"),
        ):
            claude_codex_bridge._proxy_opener("http://127.0.0.1:7897")
            handler.assert_called_with(
                {
                    "http": "http://127.0.0.1:7897",
                    "https": "http://127.0.0.1:7897",
                }
            )
            claude_codex_bridge._proxy_opener("direct")
            handler.assert_called_with({})

    def test_model_discovery_rejects_oversized_response(self) -> None:
        class LargeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                self.close()

        oversized = b"x" * (claude_codex_bridge._MAX_MODEL_RESPONSE_BYTES + 1)
        with patch.object(
            claude_codex_bridge,
            "_proxy_opener",
            return_value=Mock(open=Mock(return_value=LargeResponse(oversized))),
        ), self.assertRaisesRegex(
            claude_codex_bridge.BridgeStartupError,
            "exceeded 4 MiB",
        ):
            claude_codex_bridge.discover_anthropic_models(
                gateway_base_url="http://127.0.0.1:12345",
                local_token="local-token",
            )


class NativeClaudeDesktopCliTests(KeychainIsolationMixin):
    def test_desktop_models_command_sets_shows_and_clears_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "claude.json"
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://relay.test",
                        "desktop_discovered_models": ["claude-opus-5"],
                        "desktop_models_discovered_at": "2026-09-03T00:00:00Z",
                    }
                },
                "current": "relay",
            }
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with (
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", config_path),
                patch.object(apiagent, "SECRET_STORE", SecureStore(root / "secrets")),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    apiagent.claude_desktop_models_main(
                        ["relay", "claude-sonnet-5", "claude-sonnet-5"]
                    ),
                    0,
                )
                self.assertEqual(apiagent.claude_desktop_models_main(["relay"]), 0)
                self.assertEqual(
                    apiagent.claude_desktop_models_main(
                        ["relay", "--auto", "--1m"]
                    ),
                    0,
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("desktop_models", saved["nodes"]["relay"])
            self.assertTrue(
                saved["nodes"]["relay"]["desktop_models_support_1m"]
            )
            text = output.getvalue()
            self.assertIn("explicit override", text)
            self.assertIn("claude-opus-5", text)

    def test_desktop_models_rejects_non_claude_model(self) -> None:
        config = {"nodes": {"relay": {"base_url": "https://relay.test"}}}
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = apiagent.claude_desktop_models_main(["relay", "glm-5.2"])
        self.assertEqual(code, 1)
        self.assertIn("must start with 'claude-'", errors.getvalue())

    def test_desktop_models_context_mode_is_node_scoped_and_reversible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "claude.json"
            config_path.write_text(
                json.dumps(
                    {
                        "nodes": {
                            "relay": {
                                "base_url": "https://relay.test",
                                "desktop_models": ["claude-opus-5"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", config_path),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(
                    apiagent.claude_desktop_models_main(["relay", "--1m"]),
                    0,
                )
                self.assertEqual(apiagent.claude_desktop_models_main(["relay"]), 0)
                enabled = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertTrue(
                    enabled["nodes"]["relay"]["desktop_models_support_1m"]
                )
                self.assertEqual(
                    apiagent.claude_desktop_models_main(["relay", "--standard"]),
                    0,
                )

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertIs(
                saved["nodes"]["relay"]["desktop_models_support_1m"],
                False,
            )
            self.assertIn("Context: prefer 1M", output.getvalue())

    def test_desktop_models_rejects_1m_for_cpa_bridge_without_saving(self) -> None:
        node = {
            "type": "codex_bridge",
            "codex_profile": "relay",
            "desktop_models": ["claude-sonnet-5"],
        }
        config = {"nodes": {"bridge": node}}
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "save_claude_config") as save,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = apiagent.claude_desktop_models_main(["bridge", "--1m"])

        self.assertEqual(code, 1)
        save.assert_not_called()
        self.assertNotIn("desktop_models_support_1m", node)
        self.assertIn("only supported for native Claude nodes", errors.getvalue())

    def test_native_nodes_default_to_1m_while_bridges_require_opt_in(self) -> None:
        self.assertTrue(apiagent.claude_desktop_models_support_1m({}))
        self.assertFalse(
            apiagent.claude_desktop_models_support_1m(
                {"type": "codex_bridge"}
            )
        )
        self.assertFalse(
            apiagent.claude_desktop_models_support_1m(
                {"desktop_models_support_1m": False}
            )
        )
        self.assertFalse(
            apiagent.claude_desktop_models_support_1m(
                {
                    "type": "codex_bridge",
                    "desktop_models_support_1m": True,
                }
            )
        )

    def test_native_cli_defaults_and_explicit_models_use_1m_with_autocompact(self) -> None:
        node = {
            "desktop_models": ["claude-opus-5", "claude-fable-5-1"],
        }
        default_args, default_model = apiagent.prepare_native_claude_args(
            node,
            ["--resume", "session-id", "--permission-mode", "bypassPermissions"],
        )
        explicit_args, explicit_model = apiagent.prepare_native_claude_args(
            node,
            [
                "--model",
                "claude-fable-5-1",
                "--autocompact",
                "180k",
                "--resume",
                "session-id",
            ],
        )
        equals_args, equals_model = apiagent.prepare_native_claude_args(
            node,
            ["--model=claude-fable-5-1", "--resume", "session-id"],
        )

        self.assertEqual(default_model, "claude-opus-5[1m]")
        self.assertEqual(
            default_args,
            [
                "--autocompact",
                "auto",
                "--model",
                "claude-opus-5[1m]",
                "--resume",
                "session-id",
                "--permission-mode",
                "bypassPermissions",
            ],
        )
        self.assertEqual(explicit_model, "claude-fable-5-1[1m]")
        self.assertEqual(
            explicit_args,
            [
                "--model",
                "claude-fable-5-1[1m]",
                "--autocompact",
                "180k",
                "--resume",
                "session-id",
            ],
        )
        self.assertEqual(equals_model, "claude-fable-5-1[1m]")
        self.assertEqual(
            equals_args,
            [
                "--autocompact",
                "auto",
                "--model=claude-fable-5-1[1m]",
                "--resume",
                "session-id",
            ],
        )
        environment = apiagent.claude_native_model_environment(
            node,
            default_model,
        )
        self.assertEqual(environment["ANTHROPIC_MODEL"], "claude-opus-5[1m]")
        self.assertEqual(
            environment["ANTHROPIC_DEFAULT_OPUS_MODEL"],
            "claude-opus-5[1m]",
        )
        self.assertEqual(
            environment["ANTHROPIC_DEFAULT_FABLE_MODEL"],
            "claude-fable-5-1[1m]",
        )

    def test_native_cli_explicit_model_overrides_saved_model_preference(self) -> None:
        node = {
            "cli_force_default_model": False,
            "desktop_models": ["claude-opus-5"],
        }
        for model_args, expected_model_args in (
            (
                ["--model", "claude-fable-5-1"],
                ["--model", "claude-fable-5-1[1m]"],
            ),
            (
                ["--model=claude-fable-5-1"],
                ["--model=claude-fable-5-1[1m]"],
            ),
        ):
            with self.subTest(model_args=model_args):
                tail = ["--autocompact", "180k", "--resume", "session-id"]
                args, selected_model = apiagent.prepare_native_claude_args(
                    node, [*model_args, *tail]
                )
                self.assertEqual(args, [*expected_model_args, *tail])
                self.assertEqual(selected_model, "claude-fable-5-1[1m]")
                self.assertEqual(
                    apiagent.claude_native_model_environment(node, selected_model)[
                        "ANTHROPIC_MODEL"
                    ],
                    "claude-fable-5-1[1m]",
                )

    def test_saved_model_preference_keeps_standard_context_unchanged(self) -> None:
        node = {
            "cli_force_default_model": False,
            "desktop_models_support_1m": False,
            "desktop_models": ["claude-opus-5"],
        }
        args, selected_model = apiagent.prepare_native_claude_args(
            node, ["--resume", "session-id"]
        )

        self.assertEqual(args, ["--autocompact", "auto", "--resume", "session-id"])
        self.assertIsNone(selected_model)
        self.assertEqual(
            apiagent.claude_native_model_environment(node, selected_model), {}
        )

    def test_standard_native_node_keeps_model_bare_but_autocompacts(self) -> None:
        args, selected_model = apiagent.prepare_native_claude_args(
            {"desktop_models_support_1m": False},
            ["--model=claude-fable-5-1", "--resume", "session-id"],
        )

        self.assertIsNone(selected_model)
        self.assertEqual(
            args,
            [
                "--autocompact",
                "auto",
                "--model=claude-fable-5-1",
                "--resume",
                "session-id",
            ],
        )

    def test_native_desktop_parent_starts_worker_without_loading_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Claude.exe"
            executable.write_bytes(b"test")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://relay.test",
                        "credential_id": "claude:relay",
                    }
                },
                "current": "relay",
            }
            with (
                patch.object(apiagent.os, "name", "nt"),
                patch.object(apiagent, "CLAUDE_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "ensure_private_desktop_directory"),
                patch.object(
                    apiagent,
                    "find_claude_desktop_executable",
                    return_value=executable,
                ),
                patch.object(
                    apiagent,
                    "_spawn_claude_desktop_worker",
                    return_value=0,
                ) as spawn,
                patch.object(
                    apiagent,
                    "get_claude_secret",
                    side_effect=AssertionError("parent loaded upstream token"),
                ),
                patch.object(
                    apiagent,
                    "get_or_create_claude_desktop_bridge_token",
                    side_effect=AssertionError("parent loaded local token"),
                ),
                patch.object(apiagent, "save_claude_config"),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.launch_claude_desktop_bridge(config, "relay")

            self.assertEqual(code, 0)
            spawn.assert_called_once_with(root / "desktop" / "relay", "relay", port=None)

    def test_native_desktop_worker_discovers_models_without_cpa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("claude:relay", "upstream-secret")
            store.set("claude-desktop-bridge:relay", "local-token")
            executable = root / "Claude.exe"
            executable.write_bytes(b"test")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://relay.test",
                        "credential_id": "claude:relay",
                        "proxy_enabled": False,
                        "proxy_url": "http://127.0.0.1:7897",
                    }
                },
                "current": "relay",
            }
            gateway_calls: list[dict[str, object]] = []

            @contextmanager
            def fake_gateway(**kwargs):
                gateway_calls.append(kwargs)
                yield apiagent.BridgeEndpoint(
                    base_url="http://127.0.0.1:45678",
                    token="local-token",
                )

            class FakeDesktopProcess:
                pid = 4242

                def poll(self):
                    return 0

            state_writes: list[dict[str, object]] = []
            with (
                patch.object(apiagent.os, "name", "nt"),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "ensure_private_desktop_directory"),
                patch.object(
                    apiagent,
                    "find_claude_desktop_executable",
                    return_value=executable,
                ),
                patch.object(
                    apiagent,
                    "desktop_instance_lock",
                    return_value=nullcontext(),
                ),
                patch.object(apiagent, "clear_runtime_state"),
                patch.object(apiagent, "clear_startup_error"),
                patch.object(apiagent, "clear_desktop_stop_request"),
                patch.object(
                    apiagent,
                    "anthropic_passthrough_bridge",
                    fake_gateway,
                ),
                patch.object(
                    apiagent,
                    "cpa_bridge",
                    side_effect=AssertionError("native node started CPA"),
                ),
                patch.object(
                    apiagent,
                    "discover_anthropic_models",
                    return_value=["claude-sonnet-5", "claude-opus-5"],
                ) as discover,
                patch.object(apiagent, "prepare_claude_desktop_profile") as prepare,
                patch.object(
                    apiagent,
                    "sync_claude_shared_mcp",
                    return_value=(True, {}),
                ),
                patch.object(
                    apiagent,
                    "launch_claude_desktop_process",
                    return_value=FakeDesktopProcess(),
                ) as launch,
                patch.object(apiagent, "wait_for_claude_desktop_start"),
                patch.object(apiagent, "monitor_claude_desktop_process"),
                patch.object(
                    apiagent,
                    "write_runtime_state",
                    side_effect=lambda _path, state: state_writes.append(state),
                ),
                patch.object(apiagent, "save_claude_config") as save,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.run_claude_desktop_worker(
                    config,
                    "relay",
                    port=45678,
                )
                self.assertEqual(code, 0)
                discover.assert_called_once_with(
                    gateway_base_url="http://127.0.0.1:45678",
                    local_token="local-token",
                )
                discovered_prepare_kwargs = dict(prepare.call_args.kwargs)
                discovered_state = dict(state_writes[0])

                config["nodes"]["relay"]["desktop_models"] = [
                    "claude-haiku-4-5"
                ]
                discover.reset_mock()
                discover.side_effect = AssertionError(
                    "explicit Desktop models triggered discovery"
                )
                prepare.reset_mock()
                state_writes.clear()
                save.reset_mock()
                explicit_code = apiagent.run_claude_desktop_worker(
                    config,
                    "relay",
                    port=45678,
                )
                explicit_prepare_kwargs = dict(prepare.call_args.kwargs)
                explicit_state = dict(state_writes[0])

            self.assertEqual(code, 0)
            self.assertEqual(explicit_code, 0)
            self.assertEqual(gateway_calls[0]["upstream_api_key"], "upstream-secret")
            self.assertEqual(gateway_calls[0]["proxy_url"], "direct")
            discover.assert_not_called()
            self.assertEqual(
                discovered_prepare_kwargs["native_models"],
                ["claude-sonnet-5", "claude-opus-5"],
            )
            self.assertTrue(
                discovered_prepare_kwargs["native_models_support_1m"]
            )
            self.assertNotIn("upstream-secret", str(discovered_prepare_kwargs))
            self.assertEqual(
                explicit_prepare_kwargs["native_models"],
                ["claude-haiku-4-5"],
            )
            self.assertTrue(explicit_prepare_kwargs["native_models_support_1m"])
            self.assertEqual(explicit_state["models"], ["claude-haiku-4-5"])
            save.assert_not_called()
            self.assertEqual(
                launch.call_args.kwargs,
                {
                    "web_search_base_url": None,
                    "web_search_token": None,
                    "web_search_model": None,
                    "claude_config_dir": (apiagent.HOME / ".claude").resolve(),
                },
            )
            self.assertEqual(
                discovered_prepare_kwargs["claude_config_dir"],
                (apiagent.HOME / ".claude").resolve(),
            )
            self.assertEqual(discovered_state["gateway"], "anthropic")
            self.assertEqual(
                discovered_state["claudeConfigDir"],
                str((apiagent.HOME / ".claude").resolve()),
            )
            self.assertEqual(
                discovered_state["userDataDir"],
                str((root / "desktop" / "relay").resolve()),
            )
            self.assertEqual(
                discovered_state["effectiveUserDataDir"],
                str(
                    (
                        root
                        / "desktop"
                        / "relay"
                        / ".desktop-localappdata"
                        / "Claude-3p"
                    ).resolve()
                ),
            )
            self.assertNotEqual(
                discovered_state["effectiveUserDataDir"],
                discovered_state["userDataDir"],
            )
            self.assertEqual(
                discovered_state["models"],
                ["claude-sonnet-5", "claude-opus-5"],
            )
            self.assertEqual(
                config["nodes"]["relay"]["desktop_discovered_models"],
                ["claude-sonnet-5", "claude-opus-5"],
            )

    def test_status_stop_and_token_accept_native_node(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            config = {
                "nodes": {"relay": {"base_url": "https://relay.test"}},
                "current": "relay",
            }
            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CLAUDE_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "read_runtime_state", return_value=None),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(apiagent.show_claude_desktop_status(config, "relay"), 0)
                self.assertEqual(apiagent.stop_claude_desktop(config, "relay"), 0)
                self.assertEqual(
                    apiagent.show_claude_desktop_bridge_token(config, "relay"),
                    0,
                )
            self.assertIn("relay", output.getvalue())
