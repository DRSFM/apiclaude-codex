from __future__ import annotations

from contextlib import ExitStack, redirect_stderr
import io
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import apiagent
import codex_accounts
import codex_desktop_macos as macos
from secure_store import SecureStoreError


@unittest.skipUnless(sys.platform == "darwin", "macOS Desktop")
class MacOSDesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(apiagent.sys, "platform", "darwin"))
        for name, value in (
            ("HOME", self.root), ("CODEX_HOME", self.root / ".codex-api"),
            ("CODEX_DESKTOP_DATA_ROOT", self.root / ".apicodex-desktop"),
        ):
            self.stack.enter_context(patch.object(apiagent, name, value))
        self.stack.enter_context(patch.dict(os.environ, {"APICODEX_DESKTOP_EXE": ""}))

    def bundle(self, name="ChatGPT.app", identifier="com.openai.codex", executable="ChatGPT"):
        bundle = self.root / "Applications" / name
        binary = bundle / "Contents" / "MacOS" / executable
        binary.parent.mkdir(parents=True)
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": identifier, "CFBundleExecutable": executable,
        }))
        return bundle, binary

    def profile(self):
        profile = {"id": "relay", "name": "relay", "home": "profiles/relay",
                   "credentialId": "codex:relay", "baseUrl": "https://example.test/v1"}
        home = apiagent.codex_profile_home(profile)
        home.mkdir(parents=True)
        (home / "config.toml").write_text('model = "saved"\ncli_auth_credentials_store = "ephemeral"\n')
        return profile, home

    def mock_launch(self):
        mocks = {}
        for name, value in (
            ("find_codex_desktop_executable", self.root / "ChatGPT"),
            ("get_codex_secret", "synthetic-key"), ("ensure_codex_keyring_auth", True),
            ("prepare_codex_vision_runtime", True), ("start_detached_process", 0),
            ("repair_codex_home_images", None), ("auto_refresh_codex_models", None),
            ("sync_codex_shared_mcp", None), ("add_current_project_trust", None),
            ("update_codex_last_used", None), ("label_codex_desktop_window", True),
            ("register_macos_codex_desktop", True),
            ("save_codex_profiles", None),
        ):
            mocks[name] = self.stack.enter_context(patch.object(apiagent, name, return_value=value))
        mocks["getpass"] = self.stack.enter_context(patch.object(apiagent, "getpass", side_effect=AssertionError("unexpected credential prompt")))
        return mocks

    def test_discovers_codex_in_chatgpt_bundle(self):
        _, binary = self.bundle()
        self.assertEqual(apiagent.find_codex_desktop_executable(), binary)

    def test_classic_chatgpt_is_skipped_for_codex_bundle(self):
        self.bundle(identifier="com.openai.chat")
        _, binary = self.bundle("Codex.app", executable="Codex")
        self.assertEqual(apiagent.find_codex_desktop_executable(), binary)

    def test_bundle_and_executable_overrides_and_invalid_override(self):
        bundle, binary = self.bundle("Custom Name.app")
        for target in (bundle, binary):
            with self.subTest(target=target), patch.dict(os.environ, {"APICODEX_DESKTOP_EXE": str(target)}):
                self.assertEqual(apiagent.find_codex_desktop_executable(), binary)
        with patch.dict(os.environ, {"APICODEX_DESKTOP_EXE": str(self.root / "missing.app")}):
            self.assertIsNone(apiagent.find_codex_desktop_executable())

    def test_bundle_rejects_malformed_manifest_and_path_escape(self):
        bundle, _ = self.bundle()
        manifest = bundle / "Contents" / "Info.plist"
        for value in ([], {"CFBundleIdentifier": "com.openai.codex", "CFBundleExecutable": "../../../elsewhere"}):
            manifest.write_bytes(plistlib.dumps(value))
            self.assertIsNone(macos.bundle_executable(bundle))
        manifest.write_bytes(b"broken plist")
        self.assertIsNone(macos.bundle_executable(bundle))

    def test_api_launch_isolates_both_homes_and_parent_runtime(self):
        profile, home = self.profile()
        mocks = self.mock_launch()
        account = self.root / ".codex"
        account.mkdir()
        marker = account / "auth.json"
        marker.write_text("account-state-must-stay")
        inherited = {"CODEX_SQLITE_HOME": "parent-db", "CODEX_CLI_PATH": "parent-cli",
                     "CODEX_ELECTRON_USER_DATA_PATH": "parent-ui", "CODEX_APP_TOOLS_PIPE_PATH": "parent-pipe",
                     "CODEX_ACCESS_TOKEN": "parent-token", "ELECTRON_RUN_AS_NODE": "1",
                     "APICODEX_DREAM_SKIN_SCRIPT": "windows-only.ps1"}
        with patch.dict(os.environ, inherited):
            self.assertEqual(apiagent.launch_codex_desktop([profile], profile), 0)
        start = mocks["start_detached_process"]
        env = start.call_args.kwargs["env"]
        data = apiagent.CODEX_DESKTOP_DATA_ROOT / "relay"
        self.assertEqual(env, {"CODEX_HOME": str(home), "CODEX_ELECTRON_USER_DATA_PATH": str(data), "APICODEX_API_KEY": "synthetic-key"})
        self.assertTrue(set(inherited) <= set(start.call_args.kwargs["env_remove"]))
        self.assertEqual(start.call_args.args[1], [f"--user-data-dir={data}"])
        self.assertNotIn("synthetic-key", repr(start.call_args.args))
        self.assertEqual(marker.read_text(), "account-state-must-stay")
        mocks["label_codex_desktop_window"].assert_not_called()
        mocks["register_macos_codex_desktop"].assert_called_once_with(apiagent.CODEX_DESKTOP_DATA_ROOT, data, "relay", self.root / "ChatGPT")
        mocks["ensure_codex_keyring_auth"].assert_called_once_with(home, "synthetic-key", profile)

    def test_storage_error_preserves_credential_and_does_not_launch(self):
        profile, _ = self.profile()
        mocks = self.mock_launch()
        mocks["get_codex_secret"].side_effect = SecureStoreError("Keychain unavailable")
        with redirect_stderr(io.StringIO()):
            self.assertEqual(apiagent.launch_codex_desktop([profile], profile), 1)
        for name in ("getpass", "save_codex_profiles", "ensure_codex_keyring_auth", "start_detached_process"):
            mocks[name].assert_not_called()

    def test_redirected_desktop_directory_fails_before_secret_access(self):
        profile, _ = self.profile()
        mocks = self.mock_launch()
        data = apiagent.CODEX_DESKTOP_DATA_ROOT
        data.mkdir()
        (data / "relay").symlink_to(self.root, target_is_directory=True)
        with redirect_stderr(io.StringIO()):
            self.assertEqual(apiagent.launch_codex_desktop([profile], profile), 1)
        mocks["get_codex_secret"].assert_not_called()
        mocks["start_detached_process"].assert_not_called()

    def test_keyring_login_uses_stdin_and_preserves_config(self):
        profile, home = self.profile()
        with patch.object(apiagent, "find_codex_cli_executable", return_value="/official/codex"), patch.object(apiagent, "codex_auth_decryption_failed", return_value=False), patch.object(apiagent, "run_command", return_value=0) as run:
            self.assertTrue(apiagent.ensure_codex_keyring_auth(home, "\ufeffsynthetic-key", profile))
        self.assertEqual(run.call_args.args, ("/official/codex", ["login", "--with-api-key"]))
        self.assertEqual(run.call_args.kwargs["input_text"], "synthetic-key\n")
        self.assertEqual(run.call_args.kwargs["env"]["CODEX_HOME"], str(home))
        self.assertFalse((home / "auth.json").exists())
        self.assertIn('model = "saved"', (home / "config.toml").read_text())
        self.assertIn('cli_auth_credentials_store = "keyring"', (home / "config.toml").read_text())

    def test_failed_keyring_login_does_not_start_desktop(self):
        profile, _ = self.profile()
        mocks = self.mock_launch()
        mocks["ensure_codex_keyring_auth"].return_value = False
        self.assertEqual(apiagent.launch_codex_desktop([profile], profile), 1)
        mocks["start_detached_process"].assert_not_called()

    def test_native_process_detaches_and_reports_immediate_failure(self):
        _, binary = self.bundle()
        process = Mock()
        process.wait.side_effect = subprocess.TimeoutExpired(str(binary), 1)
        with patch.object(apiagent.subprocess, "Popen", return_value=process) as popen, patch.object(apiagent, "activate_macos_codex_desktop", return_value=True) as activate:
            args = [f"--user-data-dir={self.root / 'data'}"]
            self.assertEqual(apiagent.start_detached_process(str(binary), args, env={"CODEX_HOME": str(self.root)}), 0)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            activate.assert_called_once_with(process.pid)
            process.wait.side_effect = None
            process.wait.return_value = 7
            with redirect_stderr(io.StringIO()):
                self.assertEqual(apiagent.start_detached_process(str(binary), args), 1)

    def test_default_account_activates_bundle_without_forcing_new_instance(self):
        bundle, binary = self.bundle()
        with patch.object(apiagent, "find_codex_desktop_executable", return_value=binary), patch.object(apiagent.subprocess, "run", return_value=Mock(returncode=0)) as run, patch.object(apiagent.subprocess, "Popen") as popen, patch.dict(os.environ, {"CODEX_ELECTRON_USER_DATA_PATH": "parent-data", "ELECTRON_RUN_AS_NODE": "1"}):
            self.assertEqual(codex_accounts.launch_default([], apiagent, desktop=True), 0)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/open", "-a", str(bundle), "--env", f"CODEX_HOME={self.root / '.codex'}"])
        self.assertNotIn("CODEX_ELECTRON_USER_DATA_PATH", run.call_args.kwargs["env"])
        self.assertNotIn("ELECTRON_RUN_AS_NODE", run.call_args.kwargs["env"])
        popen.assert_not_called()

    def test_named_account_desktop_reuses_auth_without_api_login(self):
        profile, home = self.profile()
        profile["type"] = "chatgpt"
        (home / "config.toml").write_text('model_provider = "openai"\nforced_login_method = "chatgpt"\ncli_auth_credentials_store = "keyring"\n')
        secret = home / "secrets" / "codex_auth.age"
        secret.parent.mkdir()
        secret.write_bytes(b"existing-encrypted-auth")
        mocks = self.mock_launch()
        with patch.object(codex_accounts, "sync_resources"):
            self.assertEqual(codex_accounts._launch(profile, [], apiagent, desktop=True), 0)
        env = mocks["start_detached_process"].call_args.kwargs["env"]
        self.assertEqual(env["CODEX_ELECTRON_USER_DATA_PATH"], str(apiagent.CODEX_DESKTOP_DATA_ROOT / "relay"))
        self.assertNotIn("APICODEX_API_KEY", env)
        self.assertEqual(secret.read_bytes(), b"existing-encrypted-auth")
        mocks["get_codex_secret"].assert_not_called()
        mocks["ensure_codex_keyring_auth"].assert_not_called()
        mocks["register_macos_codex_desktop"].assert_called_once()
