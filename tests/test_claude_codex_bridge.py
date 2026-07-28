from __future__ import annotations

import io
import json
import os
import socket
import struct
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import apiagent
import claude_codex_bridge
from secure_store import SecureStore


class ClaudeCodexBridgeTests(unittest.TestCase):
    def test_bridge_rejects_litellm_version_with_usage_warning_bug(self) -> None:
        with (
            patch.object(
                claude_codex_bridge.importlib.util,
                "find_spec",
                return_value=object(),
            ),
            patch.object(
                claude_codex_bridge.importlib_metadata,
                "version",
                return_value="1.83.3",
            ),
            self.assertRaisesRegex(
                claude_codex_bridge.BridgeStartupError,
                "1.93.0 or newer",
            ),
        ):
            with claude_codex_bridge.litellm_bridge(
                upstream_base_url="https://upstream.test/v1",
                upstream_api_key="sk-test",
                model="gpt-test",
            ):
                pass

    def test_bridge_silences_a_client_connection_reset(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            with claude_codex_bridge.litellm_bridge(
                upstream_base_url="https://upstream.test/v1",
                upstream_api_key="sk-test",
                model="gpt-test",
            ) as endpoint:
                port = int(endpoint.base_url.rsplit(":", 1)[1])
                client = socket.create_connection(("127.0.0.1", port), timeout=2)
                client.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_LINGER,
                    struct.pack("hh", 1, 0),
                )
                client.close()
                time.sleep(0.05)

        self.assertNotIn("ConnectionResetError", errors.getvalue())

    def test_cpa_auth_shim_silences_a_windows_connection_abort(self) -> None:
        upstream_response = Mock()
        upstream_response.status = 200
        upstream_response.headers = {}
        upstream_response.read.side_effect = [b"data: test\n\n", b""]
        opener = Mock()
        opener.open.return_value = upstream_response

        handler = object.__new__(claude_codex_bridge._AuthShimRequestHandler)
        handler.path = "/responses"
        handler.headers = {
            "Authorization": (
                f"Bearer {claude_codex_bridge.CPA_SHIM_API_KEY}"
            ),
            "Content-Length": "2",
        }
        handler.rfile = io.BytesIO(b"{}")
        handler.wfile = Mock()
        handler.wfile.write.side_effect = ConnectionAbortedError(
            10053,
            "An established connection was aborted by the software in your host machine",
        )
        handler.server = SimpleNamespace(
            upstream_base_url="https://upstream.test/v1",
            upstream_api_key="sk-test",
            proxy_url="direct",
        )
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()

        with patch.object(
            claude_codex_bridge.urllib_request,
            "build_opener",
            return_value=opener,
        ):
            handler.do_POST()

        upstream_response.close.assert_called_once()
        self.assertTrue(handler.close_connection)

    def test_add_bridge_node_reuses_profile_metadata_without_copying_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_home = root / ".codex-api" / "profiles" / "relay"
            profile_home.mkdir(parents=True)
            (profile_home / "config.toml").write_text(
                'model = "gpt-test"\nmodel_reasoning_effort = "high"\n',
                encoding="utf-8",
            )
            (root / "cli-proxy-api.exe").write_bytes(b"test")
            profile = {
                "id": "relay",
                "name": "relay",
                "baseUrl": "https://openai-compatible.test/v1",
                "home": "profiles/relay",
                "credentialId": "codex:relay",
            }
            config = {"nodes": {}, "current": None}

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "save_claude_config") as save,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.add_claude_codex_bridge(
                    config,
                    "relay",
                    node_name="gpt-shell",
                    cpa_executable=root / "cli-proxy-api.exe",
                    proxy_url="http://127.0.0.1:7897",
                )

            self.assertEqual(code, 0)
            node = config["nodes"]["gpt-shell"]
            self.assertEqual(node["type"], "codex_bridge")
            self.assertEqual(node["codex_profile"], "relay")
            self.assertEqual(node["model"], "gpt-test")
            self.assertEqual(node["gateway"], "cpa")
            self.assertEqual(
                node["cpa_executable"],
                str((root / "cli-proxy-api.exe").resolve()),
            )
            self.assertEqual(node["proxy_url"], "http://127.0.0.1:7897")
            self.assertEqual(node["isolation"], "isolated")
            self.assertEqual(node["home"], "nodes/gpt-shell")
            self.assertNotIn("token", node)
            self.assertNotIn("credential_id", node)
            self.assertNotIn("credentialId", node)
            save.assert_called_once_with(config)

    def test_bridge_command_routes_explicit_profile_name_and_model(self) -> None:
        config = {"nodes": {}, "current": None}
        with (
            patch.object(apiagent, "load_claude_config", return_value=config),
            patch.object(apiagent, "add_claude_codex_bridge", return_value=0) as add,
        ):
            code = apiagent.claude_main(
                [
                    "bridge",
                    "relay",
                    "--name",
                    "gpt-shell",
                    "--model",
                    "gpt-override",
                    "--cpa-exe",
                    "C:/tools/cli-proxy-api.exe",
                    "--proxy-url",
                    "http://127.0.0.1:7897",
                ]
            )

        self.assertEqual(code, 0)
        add.assert_called_once_with(
            config,
            "relay",
            node_name="gpt-shell",
            model="gpt-override",
            cpa_executable=Path("C:/tools/cli-proxy-api.exe"),
            proxy_url="http://127.0.0.1:7897",
        )

    def test_bridge_run_uses_cpa_with_ephemeral_token_and_codex_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("codex:relay", "sk-upstream-secret")
            node_home = root / ".apiclaude" / "nodes" / "gpt-shell"
            node_home.mkdir(parents=True)
            (node_home / "settings.json").write_text(
                json.dumps(
                    {
                        "theme": "dark",
                        "skillOverrides": {"other-skill": "off"},
                    }
                ),
                encoding="utf-8",
            )
            profile = {
                "id": "relay",
                "name": "relay",
                "baseUrl": "https://openai-compatible.test/v1",
                "home": "profiles/relay",
                "credentialId": "codex:relay",
            }
            config = {
                "nodes": {
                    "gpt-shell": {
                        "type": "codex_bridge",
                        "codex_profile": "relay",
                        "model": "gpt-test",
                        "gateway": "cpa",
                        "cpa_executable": str(root / "cli-proxy-api.exe"),
                        "proxy_url": "http://127.0.0.1:7897",
                        "isolation": "isolated",
                        "home": "nodes/gpt-shell",
                    }
                },
                "current": None,
            }
            bridge_calls: list[dict[str, str]] = []

            @contextmanager
            def fake_cpa_bridge(**kwargs):
                bridge_calls.append(kwargs)
                yield apiagent.BridgeEndpoint(
                    base_url="http://127.0.0.1:45678",
                    token="ephemeral-local-token",
                )

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CLAUDE_NODES_ROOT", root / ".apiclaude"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "cpa_bridge", fake_cpa_bridge),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.run_claude_node(config, "gpt-shell", ["resume"])

            self.assertEqual(code, 0)
            self.assertEqual(
                bridge_calls,
                [
                    {
                        "upstream_base_url": "https://openai-compatible.test/v1",
                        "upstream_api_key": "sk-upstream-secret",
                        "model": "gpt-test",
                        "cpa_executable": str(root / "cli-proxy-api.exe"),
                        "proxy_url": "http://127.0.0.1:7897",
                    }
                ],
            )
            env = run.call_args.kwargs["env"]
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:45678")
            self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "ephemeral-local-token")
            self.assertEqual(env["ANTHROPIC_MODEL"], "gpt-test")
            self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "gpt-test")
            self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "gpt-test")
            self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gpt-test")
            self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "gpt-test")
            self.assertNotIn("sk-upstream-secret", run.call_args.args[1])
            self.assertNotIn("OPENAI_API_KEY", env)
            settings = json.loads(
                (node_home / "settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(settings["theme"], "dark")
            self.assertEqual(settings["skillOverrides"]["other-skill"], "off")
            self.assertEqual(
                settings["skillOverrides"]["claude-api"],
                "user-invocable-only",
            )

    def test_cpa_config_never_contains_the_upstream_secret(self) -> None:
        config = claude_codex_bridge._render_cpa_config(
            host="127.0.0.1",
            port=45678,
            auth_dir=Path("C:/temp/cpa-auth"),
            shim_base_url="http://127.0.0.1:45679",
            model="gpt-5.6-sol",
            local_token="desktop-local-token",
            route_model="claude-fable-5",
        )

        self.assertIn("codex-api-key:", config)
        self.assertIn('  - "desktop-local-token"', config)
        self.assertIn('"gpt-5.6-sol"', config)
        self.assertIn('alias: "claude-fable-5"', config)
        self.assertIn("disable-image-generation: true", config)
        self.assertNotIn("sk-upstream-secret", config)
        self.assertNotIn("upstream_api_key", config)

    def test_desktop_bridge_token_is_stable_and_dpapi_backed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            with patch.object(apiagent, "SECRET_STORE", store):
                first = apiagent.get_or_create_claude_desktop_bridge_token(
                    "gpt-shell"
                )
                second = apiagent.get_or_create_claude_desktop_bridge_token(
                    "gpt-shell"
                )
                other = apiagent.get_or_create_claude_desktop_bridge_token(
                    "other-shell"
                )

            self.assertEqual(first, second)
            self.assertNotEqual(first, other)
            self.assertGreaterEqual(len(first), 32)
            self.assertNotIn(first.encode("utf-8"), next(Path(tmp).glob("*.bin")).read_bytes())

    def test_desktop_launch_delegates_to_hidden_worker_without_loading_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop_exe = root / "Claude.exe"
            desktop_exe.write_bytes(b"test")
            profile = {
                "id": "relay",
                "name": "relay",
                "baseUrl": "https://openai-compatible.test/v1",
                "home": "profiles/relay",
                "credentialId": "codex:relay",
            }
            config = {
                "nodes": {
                    "gpt-shell": {
                        "type": "codex_bridge",
                        "codex_profile": "relay",
                        "model": "gpt-test",
                        "gateway": "cpa",
                        "cpa_executable": str(root / "cli-proxy-api.exe"),
                    }
                },
                "current": "gpt-shell",
            }

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(
                    apiagent,
                    "CLAUDE_DESKTOP_DATA_ROOT",
                    root / ".apiclaude-desktop" / "nodes",
                ),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "ensure_private_desktop_directory"),
                patch.object(
                    apiagent,
                    "find_claude_desktop_executable",
                    return_value=desktop_exe,
                ),
                patch.object(
                    apiagent,
                    "_spawn_claude_desktop_worker",
                    return_value=0,
                ) as spawn,
                patch.object(
                    apiagent,
                    "get_codex_secret",
                    side_effect=AssertionError("parent loaded upstream secret"),
                ),
                patch.object(
                    apiagent,
                    "get_or_create_claude_desktop_bridge_token",
                    side_effect=AssertionError("parent loaded local token"),
                ),
                redirect_stdout(io.StringIO()) as output,
            ):
                code = apiagent.launch_claude_desktop_bridge(
                    config,
                    "gpt-shell",
                    port=18765,
                )

            self.assertEqual(code, 0)
            spawn.assert_called_once_with(
                root / ".apiclaude-desktop" / "nodes" / "gpt-shell",
                "gpt-shell",
                port=18765,
            )
            self.assertNotIn("sk-upstream-secret", output.getvalue())

    def test_desktop_launch_reports_profile_directory_os_error(self) -> None:
        profile = {
            "id": "relay",
            "name": "relay",
            "baseUrl": "https://openai-compatible.test/v1",
            "home": "profiles/relay",
            "credentialId": "codex:relay",
        }
        config = {
            "nodes": {
                "gpt-shell": {
                    "type": "codex_bridge",
                    "codex_profile": "relay",
                    "model": "gpt-test",
                    "gateway": "cpa",
                    "cpa_executable": "C:/tools/cli-proxy-api.exe",
                }
            },
            "current": "gpt-shell",
        }
        errors = io.StringIO()
        with (
            patch.object(apiagent, "CODEX_HOME", Path("C:/api")),
            patch.object(
                apiagent,
                "CLAUDE_DESKTOP_DATA_ROOT",
                Path("C:/desktop/nodes"),
            ),
            patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
            patch.object(apiagent, "read_runtime_state", return_value=None),
            patch.object(
                apiagent,
                "ensure_private_desktop_directory",
                side_effect=PermissionError("profile denied"),
            ),
            redirect_stderr(errors),
        ):
            code = apiagent.launch_claude_desktop_bridge(config, "gpt-shell")

        self.assertEqual(code, 1)
        self.assertIn("profile denied", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

    def test_desktop_worker_binds_cpa_to_one_desktop_process(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SecureStore(root / "secrets")
            store.set("codex:relay", "sk-upstream-secret")
            store.set("claude-desktop-bridge:gpt-shell", "stable-local-token")
            desktop_exe = root / "Claude.exe"
            desktop_exe.write_bytes(b"test")
            profile = {
                "id": "relay",
                "name": "relay",
                "baseUrl": "https://openai-compatible.test/v1",
                "home": "profiles/relay",
                "credentialId": "codex:relay",
            }
            config = {
                "nodes": {
                    "gpt-shell": {
                        "type": "codex_bridge",
                        "codex_profile": "relay",
                        "model": "gpt-test",
                        "gateway": "cpa",
                        "cpa_executable": str(root / "cli-proxy-api.exe"),
                    }
                },
                "current": "gpt-shell",
            }
            bridge_calls: list[dict[str, object]] = []

            @contextmanager
            def fake_cpa_bridge(**kwargs):
                bridge_calls.append(kwargs)
                yield apiagent.BridgeEndpoint(
                    base_url="http://127.0.0.1:45678",
                    token="stable-local-token",
                )

            class FakeDesktopProcess:
                pid = 4242

                def poll(self):
                    return 0

            fake_process = FakeDesktopProcess()
            state_writes: list[dict[str, object]] = []
            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(
                    apiagent,
                    "CLAUDE_DESKTOP_DATA_ROOT",
                    root / ".apiclaude-desktop" / "nodes",
                ),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "ensure_private_desktop_directory"),
                patch.object(
                    apiagent,
                    "find_claude_desktop_executable",
                    return_value=desktop_exe,
                ),
                patch.object(apiagent, "cpa_bridge", fake_cpa_bridge),
                patch.object(apiagent, "prepare_claude_desktop_profile") as prepare,
                patch.object(
                    apiagent,
                    "launch_claude_desktop_process",
                    return_value=fake_process,
                ) as launch,
                patch.object(
                    apiagent,
                    "wait_for_claude_desktop_start",
                ) as wait_for_start,
                patch.object(
                    apiagent,
                    "monitor_claude_desktop_process",
                ) as monitor,
                patch.object(apiagent, "write_runtime_state", side_effect=lambda _p, s: state_writes.append(s)),
                patch.object(apiagent, "clear_runtime_state"),
                patch.object(apiagent, "clear_startup_error"),
                patch.object(apiagent, "clear_desktop_stop_request"),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.run_claude_desktop_worker(
                    config,
                    "gpt-shell",
                    port=45678,
                )

            self.assertEqual(code, 0)
            self.assertEqual(bridge_calls[0]["upstream_api_key"], "sk-upstream-secret")
            self.assertEqual(bridge_calls[0]["local_token"], "stable-local-token")
            self.assertEqual(bridge_calls[0]["listen_port"], 45678)
            self.assertEqual(bridge_calls[0]["route_model"], "claude-fable-5")
            self.assertEqual(prepare.call_args.kwargs["local_token"], "stable-local-token")
            self.assertNotIn("sk-upstream-secret", str(prepare.call_args))
            launch.assert_called_once()
            wait_for_start.assert_called_once_with(fake_process)
            monitor.assert_called_once_with(fake_process, root / ".apiclaude-desktop" / "nodes" / "gpt-shell")
            self.assertEqual(state_writes[0]["desktopPid"], 4242)
            self.assertEqual(state_writes[0]["port"], 45678)

    def test_bridge_node_rejects_vscode_until_gateway_lifecycle_is_persistent(self) -> None:
        config = {
            "nodes": {
                "gpt-shell": {
                    "type": "codex_bridge",
                    "codex_profile": "relay",
                    "model": "gpt-test",
                    "isolation": "isolated",
                    "home": "nodes/gpt-shell",
                }
            }
        }
        with (
            patch.object(apiagent, "run_command") as run,
            redirect_stderr(io.StringIO()) as errors,
        ):
            code = apiagent.launch_claude_vscode(config, "gpt-shell")

        self.assertEqual(code, 1)
        self.assertIn("CLI prototype", errors.getvalue())
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
