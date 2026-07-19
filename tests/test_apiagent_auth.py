from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore


class ApiAgentAuthTests(unittest.TestCase):
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
            self.assertNotIn('auth_credentials_store = "file"', config)

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
