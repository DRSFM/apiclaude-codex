from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore


class ApiAgentAuthTests(unittest.TestCase):
    def test_marker_in_dpapi_store_is_not_used_as_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp) / "secrets")
            store.set("codex:relay", apiagent.CODEX_API_AUTH_MARKER)
            profile = {"id": "relay", "credentialId": "codex:relay"}

            with patch.object(apiagent, "SECRET_STORE", store):
                with self.assertRaises(KeyError):
                    apiagent.get_codex_secret(profile)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows keyring test")
    def test_codex_keyring_auth_uses_stdin_and_removes_legacy_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            apiagent.write_codex_config(home, "https://example.test/v1", "test-model")
            (home / "auth.json").write_text(
                '{"OPENAI_API_KEY":"apicodex-managed-key-in-child-environment"}',
                encoding="utf-8",
            )

            with patch.object(apiagent, "run_command", return_value=0) as run:
                result = apiagent.ensure_codex_keyring_auth(home, "sk-test-secret")

            self.assertTrue(result)
            self.assertEqual(run.call_args.args, ("codex", ["login", "--with-api-key"]))
            self.assertEqual(run.call_args.kwargs["input_text"], "sk-test-secret\n")
            self.assertEqual(run.call_args.kwargs["env"], {"CODEX_HOME": str(home)})
            self.assertNotIn("sk-test-secret", " ".join(run.call_args.args[1]))
            self.assertFalse((home / "auth.json").exists())

    @unittest.skipUnless(__import__("os").name == "nt", "Windows desktop app test")
    def test_codex_desktop_launch_isolates_account_and_profile_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            account_auth = account_home / "auth.json"
            account_auth.write_text('{"account":"must-stay-untouched"}', encoding="utf-8")
            codex_home = root / ".codex-api"
            desktop_root = root / ".apicodex-desktop"
            desktop_exe = root / "app" / "ChatGPT.exe"
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
                "credentialId": "codex:relay",
            }
            store = SecureStore(root / "secrets")
            store.set("codex:relay", "sk-test-secret")

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", codex_home),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", desktop_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "find_codex_desktop_executable", return_value=desktop_exe),
                patch.object(apiagent, "ensure_codex_keyring_auth", return_value=True) as keyring,
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "start_detached_process", return_value=0) as start,
            ):
                code = apiagent.codex_main(
                    ["--desktop", "--api-profile", "relay"]
                )

            self.assertEqual(code, 0)
            self.assertEqual(
                account_auth.read_text(encoding="utf-8"),
                '{"account":"must-stay-untouched"}',
            )
            self.assertEqual(start.call_args.args[0], str(desktop_exe))
            keyring.assert_called_once_with(
                codex_home / "profiles" / "relay", "sk-test-secret"
            )
            self.assertEqual(
                start.call_args.args[1],
                [f"--user-data-dir={desktop_root / 'relay'}"],
            )
            self.assertEqual(
                start.call_args.kwargs["env"],
                {
                    "CODEX_HOME": str(codex_home / "profiles" / "relay"),
                    "APICODEX_API_KEY": "sk-test-secret",
                },
            )
            self.assertNotIn(
                "sk-test-secret",
                " ".join([start.call_args.args[0], *start.call_args.args[1]]),
            )
            self.assertIn(
                "OPENAI_API_KEY",
                start.call_args.kwargs["env_remove"],
            )
            profile_home = codex_home / "profiles" / "relay"
            self.assertFalse((profile_home / "auth.json").exists())
            self.assertIn(
                'conversationDetailMode = "STEPS_COMMANDS"',
                (profile_home / "config.toml").read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(__import__("os").name == "nt", "Windows desktop app test")
    def test_codex_desktop_can_delegate_to_instance_scoped_dream_skin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / ".codex-api"
            desktop_root = root / ".apicodex-desktop"
            desktop_exe = root / "app" / "ChatGPT.exe"
            skin_script = root / "start-dream-skin.ps1"
            skin_script.write_text("Write-Output test", encoding="utf-8")
            profile = {
                "id": "anyrouter",
                "name": "anyrouter",
                "home": "profiles/anyrouter",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
                "credentialId": "codex:anyrouter",
            }
            store = SecureStore(root / "secrets")
            store.set("codex:anyrouter", "sk-test-secret")

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", codex_home),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", desktop_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "find_codex_desktop_executable", return_value=desktop_exe),
                patch.object(apiagent, "ensure_codex_keyring_auth", return_value=True),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent.shutil, "which", return_value="C:\\pwsh.exe"),
                patch.object(apiagent, "run_command", return_value=0) as run,
                patch.object(apiagent, "start_detached_process") as start,
                patch.dict(
                    __import__("os").environ,
                    {
                        "APICODEX_DREAM_SKIN_SCRIPT": str(skin_script),
                        "APICODEX_DREAM_SKIN_PORT": "9336",
                    },
                    clear=False,
                ),
            ):
                code = apiagent.codex_main(
                    ["--desktop", "--api-profile", "anyrouter"]
                )

            self.assertEqual(code, 0)
            start.assert_not_called()
            self.assertEqual(run.call_args.args[0], "C:\\pwsh.exe")
            self.assertEqual(
                run.call_args.args[1],
                [
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(skin_script.resolve()),
                    "-InstanceId",
                    "anyrouter",
                    "-Port",
                    "9336",
                    "-ProfilePath",
                    str(desktop_root / "anyrouter"),
                    "-RestartExisting",
                ],
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["CODEX_HOME"],
                str(codex_home / "profiles" / "anyrouter"),
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["APICODEX_API_KEY"],
                "sk-test-secret",
            )
            self.assertNotIn(
                "sk-test-secret",
                " ".join([run.call_args.args[0], *run.call_args.args[1]]),
            )
            self.assertIn("APICODEX_DREAM_SKIN_SCRIPT", run.call_args.kwargs["env_remove"])

    @unittest.skipUnless(__import__("os").name == "nt", "Windows desktop app test")
    def test_codex_desktop_does_not_start_when_keyring_login_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "credentialId": "codex:relay",
            }
            store = SecureStore(root / "secrets")
            store.set("codex:relay", "sk-test-secret")

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "find_codex_desktop_executable", return_value=root / "ChatGPT.exe"),
                patch.object(apiagent, "ensure_codex_keyring_auth", return_value=False),
                patch.object(apiagent, "start_detached_process") as start,
            ):
                code = apiagent.launch_codex_desktop([profile], profile)

            self.assertEqual(code, 1)
            start.assert_not_called()

    @unittest.skipUnless(__import__("os").name == "nt", "Windows desktop app test")
    def test_codex_desktop_refuses_profile_outside_api_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            account_auth = account_home / "auth.json"
            account_auth.write_text('{"account":"must-stay-untouched"}', encoding="utf-8")
            profile = {
                "id": "unsafe",
                "name": "unsafe",
                "home": "../.codex",
                "baseUrl": "https://example.test/v1",
                "credentialId": "codex:unsafe",
            }

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "find_codex_desktop_executable") as find_desktop,
                patch.object(apiagent, "write_codex_config") as write_config,
                patch.object(apiagent, "start_detached_process") as start,
            ):
                code = apiagent.codex_main(
                    ["--desktop", "--api-profile", "unsafe"]
                )

            self.assertEqual(code, 1)
            find_desktop.assert_not_called()
            write_config.assert_not_called()
            start.assert_not_called()
            self.assertEqual(
                account_auth.read_text(encoding="utf-8"),
                '{"account":"must-stay-untouched"}',
            )

    def test_codex_vscode_launch_uses_selected_profile_and_current_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-api"
            vscode_root = root / "vscode-data"
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
                "credentialId": "codex:relay",
            }
            store = SecureStore(root / "secrets")
            store.set("codex:relay", "sk-test-secret")

            with (
                patch.object(apiagent, "CODEX_HOME", codex_home),
                patch.object(apiagent, "CODEX_VSCODE_DATA_ROOT", vscode_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile) as select,
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.codex_main(
                    ["--vscode", "--api-profile", "relay"]
                )

            self.assertEqual(code, 0)
            select.assert_called_once_with([profile], "relay")
            self.assertEqual(run.call_args.args[0], "code")
            self.assertEqual(
                run.call_args.args[1],
                [
                    "--new-window",
                    "--user-data-dir",
                    str(vscode_root / "relay"),
                    str(Path.cwd()),
                ],
            )
            self.assertEqual(
                run.call_args.kwargs["env"],
                {
                    "CODEX_HOME": str(codex_home / "profiles" / "relay"),
                    "APICODEX_API_KEY": "sk-test-secret",
                },
            )
            self.assertEqual(
                set(run.call_args.kwargs["env_remove"]),
                {
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                    "CODEX_PERMISSION_PROFILE",
                    "CODEX_SHELL",
                    "CODEX_THREAD_ID",
                },
            )

    def test_codex_upgrade_invokes_official_installer(self) -> None:
        with (
            patch.object(apiagent.shutil, "which", return_value="pwsh"),
            patch.object(apiagent, "run_command", return_value=0) as run,
        ):
            code = apiagent.codex_main(["--up"])

        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.args[0], "pwsh")
        self.assertEqual(
            run.call_args.args[1],
            [
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                apiagent.CODEX_INSTALL_SCRIPT,
            ],
        )

    def test_codex_config_uses_profile_scoped_environment_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)

            apiagent.write_codex_config(home, "https://example.test/v1", "test-model")

            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('env_key = "APICODEX_API_KEY"', config)
            self.assertIn('exclude = ["APICODEX_API_KEY"]', config)
            self.assertIn("[features]\napps = false\nplugins = false", config)
            self.assertNotIn("requires_openai_auth = true", config)
            self.assertIn('cli_auth_credentials_store = "keyring"', config)
            self.assertIn(
                '[desktop]\nconversationDetailMode = "STEPS_COMMANDS"',
                config,
            )

    def test_codex_desktop_repairs_work_mode_without_losing_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "config.toml").write_text(
                'model = "test-model"\n\n'
                '[desktop]\n'
                'conversationDetailMode = "STEPS_PROSE"\n'
                'sansFontSize = 15\n\n'
                '[mcp_servers.example]\n'
                'url = "https://example.test/mcp"\n',
                encoding="utf-8",
            )
            profile = {
                "id": "relay",
                "name": "relay",
                "credentialId": "codex:relay",
            }

            apiagent.prepare_codex_desktop_profile(home, profile)

            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('cli_auth_credentials_store = "keyring"', config)
            self.assertNotIn('conversationDetailMode = "STEPS_PROSE"', config)
            self.assertIn('conversationDetailMode = "STEPS_COMMANDS"', config)
            self.assertIn("sansFontSize = 15", config)
            self.assertIn("https://example.test/mcp", config)
            self.assertFalse((home / "auth.json").exists())

    def test_claude_legacy_token_migrates_out_of_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "claude.json"
            store = SecureStore(Path(tmp) / "secrets")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://example.test",
                        "token": "sk-test-legacy",
                    }
                },
                "current": "relay",
            }

            changed = apiagent.migrate_claude_secrets(config, store)

            self.assertTrue(changed)
            node = config["nodes"]["relay"]
            self.assertNotIn("token", node)
            self.assertEqual(node["credential_id"], "claude:relay")
            self.assertEqual(store.get("claude:relay"), "sk-test-legacy")

    def test_claude_launch_resolves_secret_without_command_line_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:relay", "sk-test-secret")
            config = {
                "nodes": {
                    "relay": {
                        "base_url": "https://example.test",
                        "credential_id": "claude:relay",
                    }
                },
                "current": "relay",
            }

            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "save_claude_config"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.run_claude_node(config, "relay", ["--version"])

            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args[1], ["--version"])
            self.assertNotIn("sk-test-secret", " ".join(run.call_args.args[1]))
            self.assertEqual(
                run.call_args.kwargs["env"]["ANTHROPIC_AUTH_TOKEN"],
                "sk-test-secret",
            )

    def test_codex_legacy_auth_migrates_and_is_sanitized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-api"
            profile_home = codex_home / "profiles" / "relay"
            profile_home.mkdir(parents=True)
            (profile_home / "auth.json").write_text(
                '{"OPENAI_API_KEY":"sk-test-legacy"}',
                encoding="utf-8",
            )
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
            }
            store = SecureStore(Path(tmp) / "secrets")

            with patch.object(apiagent, "CODEX_HOME", codex_home):
                changed = apiagent.migrate_codex_secrets([profile], store)
                apiagent.finalize_codex_migration([profile], store)

            self.assertTrue(changed)
            self.assertEqual(profile["credentialId"], "codex:relay")
            self.assertEqual(store.get("codex:relay"), "sk-test-legacy")
            sanitized = (profile_home / "auth.json").read_text(encoding="utf-8")
            self.assertNotIn("sk-test-legacy", sanitized)
            config = (profile_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('env_key = "APICODEX_API_KEY"', config)

    def test_codex_launch_resolves_secret_into_parent_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex-api"
            profile_home = codex_home / "profiles" / "relay"
            profile_home.mkdir(parents=True)
            store = SecureStore(Path(tmp) / "secrets")
            store.set("codex:relay", "sk-test-secret")
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
                "credentialId": "codex:relay",
            }

            with (
                patch.object(apiagent, "CODEX_HOME", codex_home),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.codex_main(["--api-profile", "relay", "--version"])

            self.assertEqual(code, 0)
            self.assertEqual(
                run.call_args.args[1],
                ["--disable", "apps", "--disable", "plugins", "--version"],
            )
            self.assertNotIn("sk-test-secret", " ".join(run.call_args.args[1]))
            self.assertEqual(
                run.call_args.kwargs["env"]["APICODEX_API_KEY"],
                "sk-test-secret",
            )
            self.assertEqual(
                set(run.call_args.kwargs["env_remove"]),
                {
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                    "CODEX_PERMISSION_PROFILE",
                    "CODEX_SHELL",
                    "CODEX_THREAD_ID",
                },
            )

    def test_run_command_can_remove_parent_environment_keys(self) -> None:
        completed = unittest.mock.Mock(returncode=0)
        with (
            patch.object(apiagent.shutil, "which", return_value="codex"),
            patch.object(apiagent.subprocess, "run", return_value=completed) as run,
            patch.dict(
                apiagent.os.environ,
                {
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE": "Codex Desktop",
                    "CODEX_THREAD_ID": "parent-thread",
                    "KEEP_ME": "yes",
                },
                clear=True,
            ),
        ):
            code = apiagent.run_command(
                "codex",
                ["--version"],
                env_remove=[
                    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE",
                    "CODEX_THREAD_ID",
                ],
            )

        self.assertEqual(code, 0)
        child_env = run.call_args.kwargs["env"]
        self.assertNotIn("CODEX_INTERNAL_ORIGINATOR_OVERRIDE", child_env)
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertEqual(child_env["KEEP_ME"], "yes")

    def test_run_command_calls_windows_batch_shims_through_cmd(self) -> None:
        completed = unittest.mock.Mock(returncode=0)
        with (
            patch.object(
                apiagent.shutil,
                "which",
                return_value=r"C:\Program Files\Microsoft VS Code\bin\code.cmd",
            ),
            patch.object(apiagent.subprocess, "run", return_value=completed) as run,
        ):
            code = apiagent.run_command("code", ["--version"])

        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertIsInstance(command, str)
        self.assertIn(" /d /s /c call ", command)
        self.assertIn(
            r'"C:\Program Files\Microsoft VS Code\bin\code.cmd" --version',
            command,
        )


if __name__ == "__main__":
    unittest.main()
