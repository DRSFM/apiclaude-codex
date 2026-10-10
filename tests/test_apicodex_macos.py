from __future__ import annotations

from contextlib import ExitStack, redirect_stderr
import io
from pathlib import Path
import tempfile
from unittest.mock import patch

import apiagent
from secure_store import SecureStoreError
from tests.support import KeychainIsolationMixin


class MacOSCodexCliTests(KeychainIsolationMixin):
    def setUp(self) -> None:
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.api_root = self.root / ".codex-api"
        self.home = self.api_root / "profiles" / "relay"
        self.home.mkdir(parents=True)
        self.profile = {
            "id": "relay", "name": "relay", "home": "profiles/relay",
            "baseUrl": "https://example.test/v1", "credentialId": "codex:relay",
        }
        self.original = (
            'model = "saved-model"\ncli_auth_credentials_store = "keyring"\n'
            '\n[features]\napps = false\nplugins = false\n'
            '\n[mcp_servers.custom]\ncommand = "custom-mcp"\n'
        )
        (self.home / "config.toml").write_text(self.original)
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("HOME", self.root), ("CODEX_HOME", self.api_root),
        ):
            stack.enter_context(patch.object(apiagent, name, value))
        stack.enter_context(patch.object(apiagent.sys, "platform", "darwin"))
        self.load = stack.enter_context(patch.object(apiagent, "load_codex_profiles", return_value=[self.profile]))
        self.secret = stack.enter_context(patch.object(apiagent, "get_codex_secret", return_value="test-saved-key"))
        self.prompt = stack.enter_context(patch.object(apiagent, "getpass", side_effect=AssertionError("unexpected password input")))
        self.store = stack.enter_context(patch.object(apiagent, "SECRET_STORE"))
        self.save = stack.enter_context(patch.object(apiagent, "save_codex_profiles"))
        self.refresh = stack.enter_context(patch.object(apiagent, "auto_refresh_codex_models"))
        self.vision = stack.enter_context(patch.object(apiagent, "prepare_codex_vision_runtime", return_value=True))
        self.sync = stack.enter_context(patch.object(apiagent, "sync_codex_shared_mcp"))
        self.trust = stack.enter_context(patch.object(apiagent, "add_current_project_trust"))
        self.last_used = stack.enter_context(patch.object(apiagent, "update_codex_last_used"))
        self.executable = stack.enter_context(patch.object(apiagent, "find_codex_cli_executable", return_value="/official/codex"))
        self.run = stack.enter_context(patch.object(apiagent, "run_command", return_value=0))

    def test_normal_start_preserves_arguments_config_and_saved_key(self) -> None:
        args = ["--yolo", "--model", "one-shot-model"]
        self.assertEqual(apiagent.codex_main(["--api-profile", "relay", *args]), 0)
        self.run.assert_called_once_with(
            "/official/codex", args,
            env={"CODEX_HOME": str(self.home), "APICODEX_API_KEY": "test-saved-key"},
            env_remove=apiagent.CODEX_API_ENV_REMOVE,
        )
        expected = self.original.replace('cli_auth_credentials_store = "keyring"', 'cli_auth_credentials_store = "ephemeral"')
        self.assertEqual((self.home / "config.toml").read_text(), expected)
        self.assertEqual((self.home / "config.before-macos-cli.toml").read_text(), self.original)
        self.secret.assert_called_once_with(self.profile)
        self.prompt.assert_not_called()
        self.store.set.assert_not_called()

    def test_auth_change_is_idempotent_and_retains_original_backup(self) -> None:
        for _ in range(2):
            self.assertEqual(apiagent.codex_main(["--api-profile", "relay"]), 0)
        self.assertEqual((self.home / "config.before-macos-cli.toml").read_text(), self.original)

    def test_storage_failure_does_not_ask_for_or_overwrite_a_key(self) -> None:
        self.secret.side_effect = SecureStoreError("macOS Keychain read failed with status 36")
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(apiagent.codex_main(["--api-profile", "relay"]), 1)
        self.assertIn("credential was retained", output.getvalue())
        self.prompt.assert_not_called()
        self.store.set.assert_not_called()
        self.save.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual((self.home / "config.toml").read_text(), self.original)

    def test_only_a_missing_key_prompts_once_and_saves_it(self) -> None:
        self.secret.side_effect = KeyError("missing")
        self.prompt.side_effect = None
        self.prompt.return_value = "\ufefftest-new-key"
        self.assertEqual(apiagent.codex_main(["--api-profile", "relay"]), 0)
        self.prompt.assert_called_once_with("API key: ")
        self.store.set.assert_called_once_with("codex:relay", "test-new-key")
        self.save.assert_called_once_with([self.profile])

    def test_failed_save_does_not_launch_or_change_registry(self) -> None:
        self.secret.side_effect = KeyError("missing")
        self.prompt.side_effect = None
        self.prompt.return_value = "test-new-key"
        self.store.set.side_effect = SecureStoreError("Keychain unavailable")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(apiagent.codex_main(["--api-profile", "relay"]), 1)
        self.run.assert_not_called()
        self.save.assert_not_called()

    def test_help_and_version_do_not_read_credentials_or_start_runtime(self) -> None:
        for flag in ("--version", "-V", "--help", "-h"):
            with self.subTest(flag=flag):
                self.run.reset_mock()
                self.assertEqual(apiagent.codex_main(["--api-profile", "relay", flag]), 0)
                self.run.assert_called_once_with(
                    "/official/codex", [flag], env={"CODEX_HOME": str(self.home)},
                    env_remove=apiagent.CODEX_API_ENV_REMOVE,
                )
        for mock in (self.secret, self.prompt, self.refresh, self.vision, self.sync, self.trust, self.last_used):
            mock.assert_not_called()
        self.assertEqual((self.home / "config.toml").read_text(), self.original)

    def test_missing_executable_fails_before_accessing_credentials(self) -> None:
        self.executable.return_value = None
        with redirect_stderr(io.StringIO()):
            self.assertEqual(apiagent.codex_main(["--api-profile", "relay"]), 1)
        self.secret.assert_not_called()
        self.prompt.assert_not_called()

    def test_macos_update_uses_official_updater_without_passwords(self) -> None:
        with patch.object(apiagent.os, "name", "posix"), patch.object(apiagent, "find_official_codex_cli_executable", return_value="/official/codex"):
            self.assertEqual(apiagent.codex_main(["--up"]), 0)
        self.run.assert_called_once_with("/official/codex", ["update"], env_remove=apiagent.CODEX_API_ENV_REMOVE)
        self.load.assert_not_called()
        self.secret.assert_not_called()

    def test_launcher_removes_inherited_account_authentication(self) -> None:
        for variable in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "OPENAI_BASE_URL", "CODEX_THREAD_ID", "CODEX_HOME"):
            self.assertIn(variable, apiagent.CODEX_API_ENV_REMOVE)
