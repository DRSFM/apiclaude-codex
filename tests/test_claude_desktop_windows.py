from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import apiagent
import claude_desktop_windows


class ClaudeDesktopWindowsTests(unittest.TestCase):
    def test_effective_user_data_dir_is_the_private_claude_3p_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"

            effective = (
                claude_desktop_windows.claude_desktop_effective_user_data_dir(
                    profile
                )
            )

            self.assertEqual(
                effective,
                (profile / ".desktop-localappdata" / "Claude-3p").resolve(),
            )
            self.assertNotEqual(effective, profile.resolve())

    def test_legacy_desktop_sessions_are_copied_without_removing_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "desktop" / "claude-code-config"
            target = root / "canonical"
            session_id = "11111111-1111-4111-8111-111111111111"
            source = legacy / "projects" / "D--work" / f"{session_id}.jsonl"
            source.parent.mkdir(parents=True)
            source.write_text('{"type":"user"}\n', encoding="utf-8")

            first = claude_desktop_windows.migrate_claude_desktop_sessions(
                legacy,
                target,
            )
            second = claude_desktop_windows.migrate_claude_desktop_sessions(
                legacy,
                target,
            )

            destination = target / "projects" / "D--work" / source.name
            self.assertEqual(first, [destination])
            self.assertEqual(second, [])
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertTrue(source.exists())

    def test_migrated_session_may_grow_in_canonical_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "desktop" / "claude-code-config"
            target = root / "canonical"
            session_id = "11111111-1111-4111-8111-111111111111"
            source = legacy / "projects" / "D--work" / f"{session_id}.jsonl"
            source.parent.mkdir(parents=True)
            source.write_bytes(b'{"type":"user"}\n')

            migrated = claude_desktop_windows.migrate_claude_desktop_sessions(
                legacy,
                target,
            )
            destination = migrated[0]
            with destination.open("ab") as handle:
                handle.write(b'{"type":"assistant"}\n')

            repeated = claude_desktop_windows.migrate_claude_desktop_sessions(
                legacy,
                target,
            )

            self.assertEqual(repeated, [])
            self.assertEqual(source.read_bytes(), b'{"type":"user"}\n')
            self.assertEqual(
                destination.read_bytes(),
                b'{"type":"user"}\n{"type":"assistant"}\n',
            )

    def test_legacy_desktop_session_conflict_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "legacy"
            target = root / "target"
            session_id = "22222222-2222-4222-8222-222222222222"
            source = legacy / "projects" / "D--work" / f"{session_id}.jsonl"
            destination = target / "projects" / "D--work" / source.name
            source.parent.mkdir(parents=True)
            destination.parent.mkdir(parents=True)
            source.write_text("source\n", encoding="utf-8")
            destination.write_text("target\n", encoding="utf-8")

            with self.assertRaisesRegex(
                claude_desktop_windows.ClaudeDesktopError,
                "conflicting transcript",
            ):
                claude_desktop_windows.migrate_claude_desktop_sessions(
                    legacy,
                    target,
                )

            self.assertEqual(source.read_text(encoding="utf-8"), "source\n")
            self.assertEqual(destination.read_text(encoding="utf-8"), "target\n")

    def test_private_acl_check_requires_exact_non_inherited_allow_list(self) -> None:
        current_sid = "S-1-5-21-test"
        report = {
            "Protected": True,
            "CurrentUserSid": current_sid,
            "Rules": [
                {
                    "Sid": sid,
                    "Type": "Allow",
                    "Rights": "FullControl",
                    "Inherited": False,
                }
                for sid in (current_sid, "S-1-5-18", "S-1-5-32-544")
            ],
        }

        self.assertTrue(claude_desktop_windows._desktop_acl_is_private(report))
        report["Rules"].append(
            {
                "Sid": "S-1-5-11",
                "Type": "Allow",
                "Rights": "ReadAndExecute",
                "Inherited": False,
            }
        )
        self.assertFalse(claude_desktop_windows._desktop_acl_is_private(report))

    def test_private_acl_is_idempotent_when_policy_already_matches(self) -> None:
        current_sid = "S-1-5-21-test"
        report = {
            "Protected": True,
            "CurrentUserSid": current_sid,
            "Rules": [
                {
                    "Sid": sid,
                    "Type": "Allow",
                    "Rights": "FullControl",
                    "Inherited": False,
                }
                for sid in (current_sid, "S-1-5-18", "S-1-5-32-544")
            ],
        }
        completed = Mock(
            returncode=0,
            stdout=json.dumps(report),
            stderr="",
        )
        with (
            patch.object(claude_desktop_windows.os, "name", "nt"),
            patch.object(
                claude_desktop_windows.shutil,
                "which",
                return_value="pwsh.exe",
            ),
            patch.object(Path, "mkdir"),
            patch.object(
                claude_desktop_windows.subprocess,
                "run",
                return_value=completed,
            ) as run,
        ):
            claude_desktop_windows.ensure_private_desktop_directory(
                Path("C:/profiles/node-a")
            )

        run.assert_called_once()
        self.assertIn(
            "ConvertTo-Json",
            run.call_args.args[0][-1],
        )

    def test_node_profiles_keep_gateway_tokens_and_history_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile_a = root / "node-a"
            profile_b = root / "node-b"
            with patch.object(
                claude_desktop_windows,
                "ensure_private_desktop_directory",
            ):
                claude_desktop_windows.prepare_claude_desktop_profile(
                    profile_a,
                    node_name="node-a",
                    gateway_base_url="http://127.0.0.1:41001",
                    local_token="token-a",
                    model="gpt-a",
                    extra_models=[
                        "claude-sonnet-5",
                        "claude-sonnet-4-6",
                        "claude-haiku-4-5",
                    ],
                )
                claude_desktop_windows.prepare_claude_desktop_profile(
                    profile_b,
                    node_name="node-b",
                    gateway_base_url="http://127.0.0.1:41002",
                    local_token="token-b",
                    model="gpt-b",
                )

            meta_a = json.loads(
                (profile_a / "configLibrary" / "_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            meta_b = json.loads(
                (profile_b / "configLibrary" / "_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            gateway_a = json.loads(
                (
                    profile_a
                    / "configLibrary"
                    / f"{meta_a['appliedId']}.json"
                ).read_text(encoding="utf-8")
            )
            gateway_b = json.loads(
                (
                    profile_b
                    / "configLibrary"
                    / f"{meta_b['appliedId']}.json"
                ).read_text(encoding="utf-8")
            )

            self.assertNotEqual(meta_a["appliedId"], meta_b["appliedId"])
            self.assertEqual(gateway_a["inferenceGatewayApiKey"], "token-a")
            self.assertEqual(gateway_b["inferenceGatewayApiKey"], "token-b")
            self.assertEqual(
                gateway_a["inferenceGatewayBaseUrl"],
                "http://127.0.0.1:41001",
            )
            self.assertEqual(
                gateway_b["inferenceGatewayBaseUrl"],
                "http://127.0.0.1:41002",
            )
            self.assertEqual(gateway_a["inferenceModels"][0]["labelOverride"], "gpt-a")
            self.assertEqual(gateway_b["inferenceModels"][0]["labelOverride"], "gpt-b")
            self.assertEqual(
                [entry["name"] for entry in gateway_a["inferenceModels"]],
                [
                    "claude-fable-5",
                    "claude-sonnet-5",
                    "claude-sonnet-4-6",
                    "claude-haiku-4-5",
                ],
            )
            self.assertNotIn("supports1m", gateway_a["inferenceModels"][0])
            self.assertNotIn("prefer1m", gateway_a["inferenceModels"][0])
            self.assertEqual(
                gateway_a["inferenceModels"][1]["anthropicFamilyTier"],
                "sonnet",
            )
            self.assertTrue(gateway_a["inferenceModels"][1]["isFamilyDefault"])
            self.assertEqual(
                gateway_a["inferenceModels"][2]["anthropicFamilyTier"],
                "sonnet",
            )
            self.assertNotIn(
                "isFamilyDefault",
                gateway_a["inferenceModels"][2],
            )
            self.assertEqual(
                gateway_a["inferenceModels"][3]["anthropicFamilyTier"],
                "haiku",
            )
            self.assertTrue(gateway_a["inferenceModels"][3]["isFamilyDefault"])
            local_3p_a = (
                claude_desktop_windows.claude_desktop_effective_user_data_dir(
                    profile_a
                )
            )
            local_3p_b = (
                claude_desktop_windows.claude_desktop_effective_user_data_dir(
                    profile_b
                )
            )
            self.assertEqual(
                json.loads(
                    (local_3p_a / "claude_desktop_config.json").read_text(
                        encoding="utf-8"
                    )
                )["deploymentMode"],
                "3p",
            )
            local_meta_a = json.loads(
                (local_3p_a / "configLibrary" / "_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            local_meta_b = json.loads(
                (local_3p_b / "configLibrary" / "_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            local_gateway_a = json.loads(
                (
                    local_3p_a
                    / "configLibrary"
                    / f"{local_meta_a['appliedId']}.json"
                ).read_text(encoding="utf-8")
            )
            local_gateway_b = json.loads(
                (
                    local_3p_b
                    / "configLibrary"
                    / f"{local_meta_b['appliedId']}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(local_gateway_a, gateway_a)
            self.assertEqual(local_gateway_b, gateway_b)
            self.assertNotEqual(
                local_gateway_a["inferenceGatewayApiKey"],
                local_gateway_b["inferenceGatewayApiKey"],
            )
            desktop_a = json.loads(
                (profile_a / "claude_desktop_config.json").read_text(encoding="utf-8")
            )
            desktop_b = json.loads(
                (profile_b / "claude_desktop_config.json").read_text(encoding="utf-8")
            )
            self.assertEqual(desktop_a["deploymentMode"], "3p")
            self.assertNotEqual(
                desktop_a["coworkUserFilesPath"],
                desktop_b["coworkUserFilesPath"],
            )
            claude_code_a = json.loads(
                (
                    profile_a
                    / "claude-code-config"
                    / ".claude.json"
                ).read_text(encoding="utf-8")
            )
            claude_code_b = json.loads(
                (
                    profile_b
                    / "claude-code-config"
                    / ".claude.json"
                ).read_text(encoding="utf-8")
            )
            mcp_a = claude_code_a["mcpServers"]["apiclaude-web"]
            mcp_b = claude_code_b["mcpServers"]["apiclaude-web"]
            self.assertEqual(mcp_a, mcp_b)
            self.assertEqual(mcp_a["type"], "stdio")
            self.assertTrue(Path(mcp_a["command"]).is_absolute())
            self.assertEqual(
                Path(mcp_a["args"][0]).name,
                "claude_gateway_mcp.py",
            )
            self.assertNotIn("token-a", json.dumps(claude_code_a))
            self.assertNotIn("token-b", json.dumps(claude_code_b))

    def test_native_profile_uses_identity_model_routes_without_fable_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "native"
            with patch.object(
                claude_desktop_windows,
                "ensure_private_desktop_directory",
            ):
                claude_desktop_windows.prepare_claude_desktop_profile(
                    profile,
                    node_name="native",
                    gateway_base_url="http://127.0.0.1:41001",
                    local_token="local-token",
                    model="claude-sonnet-5",
                    native_models=[
                        "claude-sonnet-5",
                        "claude-opus-5",
                        "claude-sonnet-5",
                    ],
                    native_models_support_1m=True,
                )

            meta = json.loads(
                (profile / "configLibrary" / "_meta.json").read_text(
                    encoding="utf-8"
                )
            )
            gateway = json.loads(
                (
                    profile
                    / "configLibrary"
                    / f"{meta['appliedId']}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                [entry["name"] for entry in gateway["inferenceModels"]],
                ["claude-sonnet-5", "claude-opus-5"],
            )
            self.assertNotIn(
                claude_desktop_windows.CLAUDE_DESKTOP_ROUTE_MODEL,
                [entry["name"] for entry in gateway["inferenceModels"]],
            )
            self.assertTrue(gateway["inferenceModels"][0]["isFamilyDefault"])
            self.assertTrue(gateway["inferenceModels"][1]["isFamilyDefault"])
            self.assertTrue(gateway["inferenceModels"][0]["supports1m"])
            self.assertTrue(gateway["inferenceModels"][0]["prefer1m"])
            self.assertTrue(gateway["inferenceModels"][1]["supports1m"])
            self.assertNotIn("prefer1m", gateway["inferenceModels"][1])

    def test_profile_update_preserves_existing_preferences_and_library_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "node"
            library = profile / "configLibrary"
            library.mkdir(parents=True)
            (profile / "claude_desktop_config.json").write_text(
                json.dumps({"preferences": {"theme": "dark"}}),
                encoding="utf-8",
            )
            (library / "_meta.json").write_text(
                json.dumps(
                    {
                        "appliedId": "manual",
                        "entries": [{"id": "manual", "name": "Manual"}],
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(
                claude_desktop_windows,
                "ensure_private_desktop_directory",
            ):
                claude_desktop_windows.prepare_claude_desktop_profile(
                    profile,
                    node_name="node",
                    gateway_base_url="http://127.0.0.1:42001",
                    local_token="local-token",
                    model="gpt-test",
                )

            desktop = json.loads(
                (profile / "claude_desktop_config.json").read_text(encoding="utf-8")
            )
            meta = json.loads((library / "_meta.json").read_text(encoding="utf-8"))
            self.assertEqual(desktop["preferences"], {"theme": "dark"})
            self.assertIn("manual", {entry["id"] for entry in meta["entries"]})
            self.assertNotEqual(meta["appliedId"], "manual")

    def test_profile_writes_gateway_mcp_to_canonical_claude_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "desktop"
            canonical = root / "node-home"
            with patch.object(
                claude_desktop_windows,
                "ensure_private_desktop_directory",
            ):
                claude_desktop_windows.prepare_claude_desktop_profile(
                    profile,
                    node_name="node",
                    gateway_base_url="http://127.0.0.1:42001",
                    local_token="local-token",
                    model="claude-opus-5",
                    native_models=["claude-opus-5"],
                    claude_config_dir=canonical,
                )

            payload = json.loads(
                (canonical / ".claude.json").read_text(encoding="utf-8")
            )
            self.assertIn("apiclaude-web", payload["mcpServers"])
            self.assertFalse((profile / "claude-code-config").exists())

    def test_profile_rejects_non_loopback_gateway(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(
                claude_desktop_windows.ClaudeDesktopError,
                "loopback",
            ):
                claude_desktop_windows.prepare_claude_desktop_profile(
                    Path(tmp),
                    node_name="node",
                    gateway_base_url="https://gateway.example/v1",
                    local_token="local-token",
                    model="gpt-test",
                )

    def test_direct_launch_sets_isolated_user_data_and_code_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            executable = root / "Claude.exe"
            executable.write_bytes(b"test")
            profile = root / "profile"
            profile.mkdir()
            fake_process = Mock()
            with (
                patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_API_KEY": "anthropic-secret",
                        "OPENAI_API_KEY": "openai-secret",
                        "APICODEX_API_KEY": "codex-secret",
                        "APICLAUDE_WEB_SEARCH_TOKEN": "stale-local-token",
                    },
                ),
                patch.object(
                    claude_desktop_windows.subprocess,
                    "Popen",
                    return_value=fake_process,
                ) as popen,
            ):
                returned = claude_desktop_windows.launch_claude_desktop_process(
                    executable,
                    profile,
                    web_search_base_url="http://127.0.0.1:43101",
                    web_search_token="active-local-token",
                    web_search_model="claude-fable-5",
                    claude_config_dir=root / "canonical-claude",
                )

            self.assertIs(returned, fake_process)
            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            self.assertEqual(
                command,
                [
                    str(executable.resolve()),
                    f"--user-data-dir={profile.resolve()}",
                ],
            )
            self.assertEqual(environment["CLAUDE_USER_DATA_DIR"], str(profile.resolve()))
            self.assertEqual(
                environment["LOCALAPPDATA"],
                str(
                    claude_desktop_windows.claude_desktop_effective_user_data_dir(
                        profile
                    ).parent
                ),
            )
            self.assertEqual(
                environment["CLAUDE_CONFIG_DIR"],
                str((root / "canonical-claude").resolve()),
            )
            self.assertNotIn("ANTHROPIC_API_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("APICODEX_API_KEY", environment)
            self.assertEqual(
                environment["APICLAUDE_WEB_SEARCH_BASE_URL"],
                "http://127.0.0.1:43101",
            )
            self.assertEqual(
                environment["APICLAUDE_WEB_SEARCH_TOKEN"],
                "active-local-token",
            )
            self.assertEqual(
                environment["APICLAUDE_WEB_SEARCH_MODEL"],
                "claude-fable-5",
            )

    def test_desktop_start_rejects_a_process_that_exits_immediately(self) -> None:
        process = Mock()
        process.poll.return_value = 4294930433
        process.returncode = 4294930433

        with self.assertRaisesRegex(
            claude_desktop_windows.ClaudeDesktopError,
            "existing non-isolated Claude Desktop",
        ):
            claude_desktop_windows.wait_for_claude_desktop_start(
                process,
                stability_seconds=0.1,
            )

    def test_monitor_stops_tray_process_after_window_stays_hidden(self) -> None:
        process = Mock(pid=4242)
        process.poll.return_value = None
        profile = Path("C:/profiles/node-a")
        with (
            patch.object(
                claude_desktop_windows,
                "desktop_stop_requested",
                return_value=False,
            ),
            patch.object(
                claude_desktop_windows,
                "desktop_process_has_visible_window",
                side_effect=[True, False, False],
            ),
            patch.object(
                claude_desktop_windows.time,
                "monotonic",
                side_effect=[0.0, 10.0, 12.0],
            ),
            patch.object(claude_desktop_windows.time, "sleep"),
            patch.object(
                claude_desktop_windows,
                "close_claude_desktop_process",
            ) as close,
        ):
            claude_desktop_windows.monitor_claude_desktop_process(
                process,
                profile,
                hidden_grace=1.5,
            )

        close.assert_called_once_with(process, timeout=2.0)

    def test_msix_executable_is_resolved_from_the_package_manifest(self) -> None:
        executable = Path("C:/Program Files/WindowsApps/Claude/app/Claude.exe")
        completed = Mock(
            returncode=0,
            stdout=f"{executable}\n",
            stderr="",
        )
        with (
            patch.object(
                claude_desktop_windows.shutil,
                "which",
                return_value="pwsh.exe",
            ),
            patch.object(
                claude_desktop_windows.subprocess,
                "run",
                return_value=completed,
            ) as run,
            patch.object(Path, "is_file", return_value=True),
        ):
            found = claude_desktop_windows.find_claude_desktop_executable()

        self.assertEqual(found, executable.resolve())
        command = run.call_args.args[0]
        script = command[-1]
        self.assertIn("Get-AppxPackageManifest", script)
        self.assertIn(".Executable", script)

    def test_stop_request_is_scoped_to_one_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_a = Path(tmp) / "a"
            profile_b = Path(tmp) / "b"
            claude_desktop_windows.request_desktop_stop(profile_a)

            self.assertTrue(
                claude_desktop_windows.desktop_stop_requested(profile_a)
            )
            self.assertFalse(
                claude_desktop_windows.desktop_stop_requested(profile_b)
            )

    def test_hidden_worker_command_and_environment_contain_no_api_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "profile"
            profile.mkdir()
            fake_worker = Mock(pid=9876)
            state = {
                "model": "gpt-test",
                "port": 43001,
                "desktopPid": 6789,
                "userDataDir": str(profile),
                "effectiveUserDataDir": str(profile / "effective"),
            }
            with (
                patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_AUTH_TOKEN": "anthropic-secret",
                        "OPENAI_API_KEY": "openai-secret",
                        "APICODEX_API_KEY": "codex-secret",
                    },
                ),
                patch.object(apiagent, "clear_runtime_state"),
                patch.object(apiagent, "clear_startup_error"),
                patch.object(apiagent, "clear_desktop_stop_request"),
                patch.object(
                    apiagent.subprocess,
                    "Popen",
                    return_value=fake_worker,
                ) as popen,
                patch.object(
                    apiagent,
                    "wait_for_worker_start",
                    return_value=(state, None),
                ),
                patch("builtins.print") as print_output,
            ):
                code = apiagent._spawn_claude_desktop_worker(
                    profile,
                    "node-a",
                    port=None,
                )

            self.assertEqual(code, 0)
            command = popen.call_args.args[0]
            environment = popen.call_args.kwargs["env"]
            joined_command = " ".join(command)
            self.assertIn("--desktop-worker", command)
            self.assertIn("node-a", command)
            self.assertNotIn("anthropic-secret", joined_command)
            self.assertNotIn("openai-secret", joined_command)
            self.assertNotIn("codex-secret", joined_command)
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("APICODEX_API_KEY", environment)
            self.assertTrue(
                any(
                    call.args == (f"User data: {profile / 'effective'}",)
                    for call in print_output.call_args_list
                )
            )


if __name__ == "__main__":
    unittest.main()
