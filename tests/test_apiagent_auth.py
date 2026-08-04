from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore


class ApiAgentAuthTests(unittest.TestCase):
    def test_profile_storage_slug_cannot_be_dot_path(self) -> None:
        self.assertEqual(apiagent.slugify("."), "profile")
        self.assertEqual(apiagent.slugify(".."), "profile")
        self.assertEqual(apiagent.slugify("../.."), "profile")
        self.assertEqual(apiagent.slugify("..-.."), "profile")

    def test_profile_metadata_normalizes_unsafe_id_for_desktop_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "..",
                "name": "legacy",
                "home": "profiles/legacy",
            }

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", root / ".desktop"),
            ):
                metadata = apiagent.codex_profile_metadata(profile)

            self.assertEqual(
                Path(metadata["desktopData"]),
                (root / ".desktop" / "profile").resolve(),
            )

    def test_api_profile_json_lists_only_non_sensitive_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "relay_profile",
                "name": "中继配置",
                "home": "profiles/relay_profile",
                "baseUrl": "https://example.test/v1",
                "credentialId": "codex:relay_profile",
                "apiKey": "must-not-leak",
                "createdAt": "2026-01-01T00:00:00+00:00",
                "lastUsedAt": None,
            }
            output = io.StringIO()

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", root / ".desktop"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                redirect_stdout(output),
            ):
                code = apiagent.codex_main(["--api-list", "--json"])

            self.assertEqual(code, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["schemaVersion"], 1)
            self.assertEqual(len(payload["profiles"]), 1)
            metadata = payload["profiles"][0]
            self.assertEqual(metadata["id"], "relay_profile")
            self.assertRegex(metadata["instanceId"], r"^relay-profile-[a-f0-9]{8}$")
            self.assertEqual(metadata["name"], "中继配置")
            self.assertEqual(metadata["baseUrl"], "https://example.test/v1")
            self.assertEqual(
                metadata["desktopData"],
                str((root / ".desktop" / "relay_profile").resolve()),
            )
            self.assertNotIn("credentialId", metadata)
            self.assertNotIn("apiKey", metadata)
            self.assertNotIn("must-not-leak", output.getvalue())

    def test_api_profile_json_rejects_unsafe_home_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            account_auth = account_home / "auth.json"
            account_auth.write_text('{"account":"sentinel"}', encoding="utf-8")
            profile = {
                "id": "unsafe",
                "name": "unsafe",
                "home": "../.codex",
                "baseUrl": "https://example.test/v1",
            }
            output = io.StringIO()

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                redirect_stdout(output),
            ):
                code = apiagent.codex_main(["--api-list", "--json"])

            self.assertEqual(code, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertEqual(
                account_auth.read_text(encoding="utf-8"),
                '{"account":"sentinel"}',
            )

    def test_json_flag_is_rejected_outside_profile_list(self) -> None:
        code = apiagent.codex_main(["--json"])
        self.assertEqual(code, 1)

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
                "id": "legacy_profile",
                "name": "legacy profile",
                "home": "profiles/legacy_profile",
                "baseUrl": "https://example.test/v1",
                "model": "test-model",
                "credentialId": "codex:legacy_profile",
            }
            store = SecureStore(root / "secrets")
            store.set("codex:legacy_profile", "sk-test-secret")

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
                    ["--desktop", "--api-profile", "legacy_profile"]
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
                    "legacy-profile-0b393d0d",
                    "-Port",
                    "9336",
                    "-ProfilePath",
                    str(desktop_root / "legacy_profile"),
                    "-RestartExisting",
                ],
            )
            self.assertEqual(
                run.call_args.kwargs["env"]["CODEX_HOME"],
                str(codex_home / "profiles" / "legacy_profile"),
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

            apiagent.write_codex_config(
                home,
                "https://example.test/v1",
                "test-model",
                "xhigh",
            )

            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model_reasoning_effort = "xhigh"', config)
            self.assertIn('env_key = "APICODEX_API_KEY"', config)
            self.assertIn('exclude = ["APICODEX_API_KEY"]', config)
            self.assertIn("[features]\napps = false\nplugins = false", config)
            self.assertNotIn("requires_openai_auth = true", config)
            self.assertIn('cli_auth_credentials_store = "keyring"', config)
            self.assertIn(
                '[desktop]\nconversationDetailMode = "STEPS_COMMANDS"',
                config,
            )

    def test_codex_config_uses_profile_local_model_catalog_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            catalog = home / "models.json"
            catalog.write_text('{"models": [{"slug": "vendor-model"}]}', encoding="utf-8")

            apiagent.write_codex_config(
                home,
                "https://example.test/v1",
                "vendor-model",
                "high",
            )

            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn(
                f'model_catalog_json = "{catalog.resolve().as_posix()}"',
                config,
            )

    def test_api_add_preserves_profile_model_and_reasoning_effort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "muyuanpub",
                "name": "muyuanpub",
                "home": "profiles/muyuanpub",
                "baseUrl": "https://example.test/v1",
                "model": "gpt-5.6-sol",
                "reasoningEffort": "xhigh",
                "credentialId": "codex:muyuanpub",
            }

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "SECRET_STORE"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "save_codex_profiles"),
                patch.object(apiagent, "getpass", return_value="sk-test-secret"),
                patch.object(
                    apiagent,
                    "fetch_codex_provider_models",
                    return_value=["gpt-5.6-sol"],
                    create=True,
                ),
                patch("builtins.input", side_effect=["", "", ""]),
            ):
                code = apiagent.add_codex_profile("muyuanpub")

            self.assertEqual(code, 0)
            config = (
                root / ".codex-api" / "profiles" / "muyuanpub" / "config.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('model = "gpt-5.6-sol"', config)
            self.assertIn('model_reasoning_effort = "xhigh"', config)

    def test_api_add_bare_command_fetches_models_and_guides_selection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            answers = iter(["vendor", "https://api.vendor.test/v1", "2"])
            prompts: list[str] = []

            def answer(prompt: str) -> str:
                prompts.append(prompt)
                return next(answers)

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "SECRET_STORE"),
                patch.object(apiagent, "load_codex_profiles", return_value=[]),
                patch.object(apiagent, "save_codex_profiles") as save_profiles,
                patch.object(apiagent, "getpass", return_value="sk-vendor-secret"),
                patch.object(
                    apiagent,
                    "fetch_codex_provider_models",
                    return_value=[
                        "alpha-model",
                        "DeepSeek-V4-Flash-0731",
                        "black-forest-labs/FLUX.2-klein-4B",
                    ],
                    create=True,
                ) as fetch_models,
                patch("builtins.input", side_effect=answer),
            ):
                code = apiagent.codex_main(["--api-add"])

            self.assertEqual(code, 0)
            self.assertEqual(
                prompts,
                [
                    "Profile name: ",
                    "API base URL [https://api.openai.com/v1]: ",
                    "Choose default model number or name: ",
                ],
            )
            fetch_models.assert_called_once_with(
                "https://api.vendor.test/v1",
                "sk-vendor-secret",
            )
            profile = save_profiles.call_args.args[0][0]
            self.assertEqual(profile["model"], "DeepSeek-V4-Flash-0731")
            config = (root / ".codex-api" / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "DeepSeek-V4-Flash-0731"', config)
            catalog = json.loads(
                (root / ".codex-api" / "models.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                [item["slug"] for item in catalog["models"]],
                ["DeepSeek-V4-Flash-0731", "alpha-model"],
            )
            self.assertEqual(
                [item["priority"] for item in catalog["models"]],
                [1, 2],
            )
            self.assertTrue(all(item["visibility"] == "list" for item in catalog["models"]))

    def test_api_add_accepts_generic_provider_options_and_installs_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_source = root / "vendor-models.json"
            catalog_source.write_text(
                json.dumps({"models": [{"slug": "vendor-model", "display_name": "Vendor"}]}),
                encoding="utf-8",
            )

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "SECRET_STORE") as store,
                patch.object(apiagent, "load_codex_profiles", return_value=[]),
                patch.object(apiagent, "save_codex_profiles") as save_profiles,
                patch.object(apiagent, "getpass", return_value="sk-vendor-secret"),
                patch("builtins.input", side_effect=AssertionError("prompted")),
            ):
                code = apiagent.add_codex_profile(
                    name="vendor",
                    base_url="https://api.vendor.test/responses",
                    model="vendor-model",
                    reasoning_effort="medium",
                    model_catalog=catalog_source,
                )

            self.assertEqual(code, 0)
            profile_home = root / ".codex-api"
            installed_catalog = profile_home / "models.json"
            self.assertEqual(
                json.loads(installed_catalog.read_text(encoding="utf-8")),
                json.loads(catalog_source.read_text(encoding="utf-8")),
            )
            config = (profile_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model = "vendor-model"', config)
            self.assertIn('model_reasoning_effort = "medium"', config)
            self.assertIn('base_url = "https://api.vendor.test/responses"', config)
            self.assertIn(
                f'model_catalog_json = "{installed_catalog.resolve().as_posix()}"',
                config,
            )
            store.set.assert_called_once_with("codex:vendor", "sk-vendor-secret")
            saved_profile = save_profiles.call_args.args[0][0]
            self.assertEqual(saved_profile["model"], "vendor-model")
            self.assertEqual(saved_profile["reasoningEffort"], "medium")
            self.assertEqual(saved_profile["modelCatalog"], "models.json")

    def test_api_add_rejects_catalog_without_selected_model_before_secret_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog_source = root / "wrong-models.json"
            catalog_source.write_text(
                '{"models": [{"slug": "different-model"}]}',
                encoding="utf-8",
            )

            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "SECRET_STORE") as store,
                patch.object(apiagent, "load_codex_profiles", return_value=[]),
                patch.object(apiagent, "save_codex_profiles") as save_profiles,
                patch.object(apiagent, "getpass", side_effect=AssertionError("read secret")),
                patch("builtins.input", side_effect=AssertionError("prompted")),
            ):
                code = apiagent.add_codex_profile(
                    name="vendor",
                    base_url="https://api.vendor.test/v1",
                    model="vendor-model",
                    model_catalog=catalog_source,
                )

            self.assertEqual(code, 1)
            store.set.assert_not_called()
            save_profiles.assert_not_called()
            self.assertFalse((root / ".codex-api" / "models.json").exists())

    def test_api_add_cli_routes_provider_options_only_in_add_mode(self) -> None:
        catalog = Path("C:/configs/vendor-models.json")
        with patch.object(apiagent, "add_codex_profile", return_value=0) as add_profile:
            code = apiagent.codex_main(
                [
                    "--api-add",
                    "--name",
                    "vendor",
                    "--base-url",
                    "https://api.vendor.test/v1",
                    "--model",
                    "vendor-model",
                    "--reasoning-effort",
                    "high",
                    "--model-catalog",
                    str(catalog),
                ]
            )

        self.assertEqual(code, 0)
        add_profile.assert_called_once_with(
            None,
            name="vendor",
            base_url="https://api.vendor.test/v1",
            model="vendor-model",
            reasoning_effort="high",
            model_catalog=catalog,
        )

    def test_codex_model_argument_remains_a_normal_pass_through_argument(self) -> None:
        profile = {
            "id": "relay",
            "name": "relay",
            "home": "profiles/relay",
            "baseUrl": "https://example.test/v1",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex-api" / "profiles" / "relay"
            home.mkdir(parents=True)
            (home / "config.toml").write_text("model = \"saved-model\"\n", encoding="utf-8")
            with (
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "get_codex_secret", return_value="sk-secret"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.codex_main(
                    ["--api-profile", "relay", "--model", "one-shot-model"]
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            run.call_args.args[1],
            ["--disable", "apps", "--disable", "plugins", "--model", "one-shot-model"],
        )

    def test_api_add_rejects_unsafe_existing_home_before_secret_or_config_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            account_config = account_home / "config.toml"
            account_config.write_text('model = "sentinel"\n', encoding="utf-8")
            profile = {
                "id": "unsafe",
                "name": "unsafe",
                "home": "../.codex",
                "baseUrl": "https://example.test/v1",
            }

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "SECRET_STORE") as store,
                patch.object(apiagent, "write_codex_config") as write_config,
                patch.object(apiagent, "save_codex_profiles") as save_profiles,
                patch.object(apiagent, "getpass", side_effect=AssertionError("read secret")),
                patch("builtins.input", side_effect=AssertionError("prompted")),
            ):
                code = apiagent.add_codex_profile("unsafe")

            self.assertEqual(code, 1)
            store.set.assert_not_called()
            write_config.assert_not_called()
            save_profiles.assert_not_called()
            self.assertEqual(
                account_config.read_text(encoding="utf-8"),
                'model = "sentinel"\n',
            )

    def test_api_remove_rejects_unsafe_home_before_secret_clear_or_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            account_auth = account_home / "auth.json"
            account_auth.write_text('{"account":"sentinel"}', encoding="utf-8")
            profile = {
                "id": "unsafe",
                "name": "unsafe",
                "home": "../.codex",
                "credentialId": "codex:unsafe",
            }

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "SECRET_STORE") as store,
                patch.object(apiagent, "save_codex_profiles") as save_profiles,
                patch.object(apiagent.shutil, "move") as move,
                patch("builtins.input", side_effect=AssertionError("prompted")),
            ):
                code = apiagent.remove_codex_profile("unsafe")

            self.assertEqual(code, 1)
            store.clear.assert_not_called()
            save_profiles.assert_not_called()
            move.assert_not_called()
            self.assertEqual(
                account_auth.read_text(encoding="utf-8"),
                '{"account":"sentinel"}',
            )

    def test_api_remove_normalizes_profile_id_before_archive_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            archive_root = api_root / "archived-profiles"
            profile_home = api_root / "profiles" / "legacy"
            profile_home.mkdir(parents=True)
            (profile_home / "sentinel.txt").write_text("profile", encoding="utf-8")
            account_home = root / ".codex"
            account_home.mkdir()
            account_sentinel = account_home / "auth.json"
            account_sentinel.write_text("account", encoding="utf-8")
            profile = {
                "id": "../account",
                "name": "legacy",
                "home": "profiles/legacy",
            }

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "CODEX_ARCHIVE_ROOT", archive_root),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "save_codex_profiles"),
                patch.object(apiagent, "SECRET_STORE"),
                patch("builtins.input", return_value="YES"),
            ):
                code = apiagent.remove_codex_profile("../account")

            self.assertEqual(code, 0)
            archives = list(archive_root.iterdir())
            self.assertEqual(len(archives), 1)
            self.assertTrue(archives[0].resolve().is_relative_to(archive_root.resolve()))
            self.assertTrue((archives[0] / "sentinel.txt").is_file())
            self.assertEqual(account_sentinel.read_text(encoding="utf-8"), "account")

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

    def test_vision_setup_enables_only_requested_profiles_after_key_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            store = SecureStore(root / "secrets")
            profiles = []
            for name, base_url, model in (
                ("deepseek", "https://api.deepseek.com/", "deepseek-v4-flash"),
                ("prism", "https://ai.prism.uno/v1", "gpt-5.5"),
                ("other", "https://other.test/v1", "gpt-test"),
            ):
                home = api_root / "profiles" / name
                home.mkdir(parents=True)
                apiagent.write_codex_config(home, base_url, model, "high")
                profile = {
                    "id": name,
                    "name": name,
                    "home": f"profiles/{name}",
                    "baseUrl": base_url,
                    "model": model,
                    "credentialId": f"codex:{name}",
                }
                profiles.append(profile)
            deepseek_catalog = {
                "models": [
                    {
                        "slug": "deepseek-v4-flash",
                        "input_modalities": ["text"],
                    }
                ]
            }
            (api_root / "profiles" / "deepseek" / "models.json").write_text(
                json.dumps(deepseek_catalog), encoding="utf-8"
            )
            saved: list[list[dict[str, object]]] = []

            with (
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=profiles),
                patch.object(
                    apiagent,
                    "save_codex_profiles",
                    side_effect=lambda value: saved.append(json.loads(json.dumps(value))),
                ),
                patch.object(apiagent, "getpass", return_value="gemini-secret"),
                patch.object(apiagent, "validate_gemini_vision") as validate,
                patch.object(
                    apiagent,
                    "choose_codex_vision_port",
                    side_effect=[19101, 19102],
                ),
                patch.object(
                    apiagent,
                    "ensure_codex_vision_worker",
                    return_value=True,
                ) as ensure,
                redirect_stdout(io.StringIO()) as output,
            ):
                code = apiagent.codex_vision_main(
                    ["setup", "deepseek", "prism"]
                )

            self.assertEqual(code, 0)
            validate.assert_called_once_with(
                "gemini-secret", "gemini-3.5-flash-lite"
            )
            self.assertEqual(store.get("vision:gemini"), "gemini-secret")
            self.assertEqual(len(saved), 1)
            configured = {item["name"]: item for item in saved[0]}
            self.assertTrue(configured["deepseek"]["vision"]["enabled"])
            self.assertEqual(configured["deepseek"]["vision"]["proxyPort"], 19101)
            self.assertEqual(configured["prism"]["vision"]["proxyPort"], 19102)
            self.assertNotIn("vision", configured["other"])
            self.assertNotIn("gemini-secret", json.dumps(saved))
            self.assertNotIn("gemini-secret", output.getvalue())
            self.assertEqual(ensure.call_count, 2)

            deepseek_config = (
                api_root / "profiles" / "deepseek" / "config.toml"
            ).read_text(encoding="utf-8")
            prism_config = (
                api_root / "profiles" / "prism" / "config.toml"
            ).read_text(encoding="utf-8")
            other_config = (
                api_root / "profiles" / "other" / "config.toml"
            ).read_text(encoding="utf-8")
            self.assertIn('base_url = "http://127.0.0.1:19101"', deepseek_config)
            self.assertIn('base_url = "http://127.0.0.1:19102/v1"', prism_config)
            self.assertIn("enable_request_compression = false", deepseek_config)
            self.assertIn('base_url = "https://other.test/v1"', other_config)
            catalog = json.loads(
                (api_root / "profiles" / "deepseek" / "models.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                catalog["models"][0]["input_modalities"], ["text", "image"]
            )

    def test_vision_setup_does_not_write_secret_or_profiles_when_validation_fails(self) -> None:
        profile = {
            "id": "deepseek",
            "name": "deepseek",
            "home": "profiles/deepseek",
            "baseUrl": "https://api.deepseek.com/",
        }
        errors = io.StringIO()
        with (
            patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
            patch.object(apiagent, "getpass", return_value="invalid-key"),
            patch.object(
                apiagent,
                "validate_gemini_vision",
                side_effect=ValueError("Gemini rejected the key"),
            ),
            patch.object(apiagent, "SECRET_STORE") as store,
            patch.object(apiagent, "save_codex_profiles") as save,
            redirect_stderr(errors),
        ):
            code = apiagent.codex_vision_main(["setup", "deepseek"])

        self.assertEqual(code, 1)
        self.assertIn("Gemini rejected the key", errors.getvalue())
        store.set.assert_not_called()
        save.assert_not_called()
        self.assertNotIn("vision", profile)

    def test_vision_disable_restores_upstream_config_and_text_modalities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            home = api_root / "profiles" / "deepseek"
            home.mkdir(parents=True)
            store = SecureStore(root / "secrets")
            store.set("vision:gemini", "gemini-secret")
            store.set("vision-control:deepseek", "control-secret")
            profile = {
                "id": "deepseek",
                "name": "deepseek",
                "home": "profiles/deepseek",
                "baseUrl": "https://api.deepseek.com/",
                "model": "deepseek-v4-flash",
                "credentialId": "codex:deepseek",
                "vision": {
                    "enabled": True,
                    "provider": "gemini",
                    "model": "gemini-3.5-flash-lite",
                    "credentialId": "vision:gemini",
                    "controlCredentialId": "vision-control:deepseek",
                    "proxyPort": 19101,
                },
            }
            apiagent.write_codex_config(
                home,
                "http://127.0.0.1:19101",
                "deepseek-v4-flash",
                "high",
            )
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "[features]\n",
                    "[features]\nenable_request_compression = false\n",
                    1,
                ),
                encoding="utf-8",
            )
            (home / "models.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "deepseek-v4-flash",
                                "input_modalities": ["text", "image"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            saved: list[list[dict[str, object]]] = []

            with (
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(
                    apiagent,
                    "save_codex_profiles",
                    side_effect=lambda value: saved.append(json.loads(json.dumps(value))),
                ),
                redirect_stdout(io.StringIO()),
            ):
                code = apiagent.codex_vision_main(["disable", "deepseek"])

            self.assertEqual(code, 0)
            self.assertNotIn("vision", profile)
            config = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('base_url = "https://api.deepseek.com/"', config)
            self.assertNotIn("enable_request_compression", config)
            catalog = json.loads((home / "models.json").read_text(encoding="utf-8"))
            self.assertEqual(catalog["models"][0]["input_modalities"], ["text"])
            with self.assertRaises(KeyError):
                store.get("vision-control:deepseek")
            with self.assertRaises(KeyError):
                store.get("vision:gemini")
            self.assertEqual(len(saved), 1)

    def test_vision_setup_rolls_back_new_credentials_when_profile_files_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp) / "secrets")
            profile = {
                "id": "deepseek",
                "name": "deepseek",
                "home": "profiles/deepseek",
                "baseUrl": "https://api.deepseek.com/",
            }
            errors = io.StringIO()
            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "getpass", return_value="gemini-secret"),
                patch.object(apiagent, "validate_gemini_vision"),
                patch.object(apiagent, "choose_codex_vision_port", return_value=19101),
                patch.object(
                    apiagent,
                    "configure_codex_vision_files",
                    side_effect=OSError("config denied"),
                ),
                patch.object(apiagent, "save_codex_profiles") as save,
                redirect_stderr(errors),
            ):
                code = apiagent.codex_vision_main(["setup", "deepseek"])

            self.assertEqual(code, 1)
            self.assertIn("config denied", errors.getvalue())
            self.assertNotIn("vision", profile)
            with self.assertRaises(KeyError):
                store.get("vision:gemini")
            with self.assertRaises(KeyError):
                store.get("vision-control:deepseek")
            save.assert_not_called()

    def test_vision_setup_rolls_back_when_profile_registry_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            home = api_root / "profiles" / "deepseek"
            home.mkdir(parents=True)
            apiagent.write_codex_config(
                home,
                "https://api.deepseek.com/",
                "deepseek-v4-flash",
                "high",
            )
            original_config = (home / "config.toml").read_bytes()
            store = SecureStore(root / "secrets")
            profile = {
                "id": "deepseek",
                "name": "deepseek",
                "home": "profiles/deepseek",
                "baseUrl": "https://api.deepseek.com/",
                "model": "deepseek-v4-flash",
            }
            with (
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "getpass", return_value="gemini-secret"),
                patch.object(apiagent, "validate_gemini_vision"),
                patch.object(apiagent, "choose_codex_vision_port", return_value=19101),
                patch.object(
                    apiagent,
                    "save_codex_profiles",
                    side_effect=OSError("registry denied"),
                ),
                redirect_stderr(io.StringIO()) as errors,
            ):
                code = apiagent.codex_vision_main(["setup", "deepseek"])

            self.assertEqual(code, 1)
            self.assertIn("registry denied", errors.getvalue())
            self.assertNotIn("vision", profile)
            self.assertEqual((home / "config.toml").read_bytes(), original_config)
            with self.assertRaises(KeyError):
                store.get("vision:gemini")
            with self.assertRaises(KeyError):
                store.get("vision-control:deepseek")

    def test_vision_command_routes_before_normal_codex_profile_loading(self) -> None:
        with (
            patch.object(apiagent, "codex_vision_main", return_value=0) as vision,
            patch.object(
                apiagent,
                "load_codex_profiles",
                side_effect=AssertionError("normal profile route used"),
            ),
        ):
            code = apiagent.codex_main(["vision", "status"])

        self.assertEqual(code, 0)
        vision.assert_called_once_with(["status"])

    def test_configured_vision_profile_starts_worker_before_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            home = api_root / "profiles" / "deepseek"
            home.mkdir(parents=True)
            apiagent.write_codex_config(
                home, "http://127.0.0.1:19101", "deepseek-v4-flash", "high"
            )
            profile = {
                "id": "deepseek",
                "name": "deepseek",
                "home": "profiles/deepseek",
                "baseUrl": "https://api.deepseek.com/",
                "model": "deepseek-v4-flash",
                "credentialId": "codex:deepseek",
                "vision": {
                    "enabled": True,
                    "provider": "gemini",
                    "model": "gemini-3.5-flash-lite",
                    "credentialId": "vision:gemini",
                    "controlCredentialId": "vision-control:deepseek",
                    "proxyPort": 19101,
                },
            }

            with (
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "get_codex_secret", return_value="upstream-key"),
                patch.object(
                    apiagent, "ensure_codex_vision_worker", return_value=True
                ) as ensure,
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.codex_main(
                    ["--api-profile", "deepseek", "--version"]
                )

            self.assertEqual(code, 0)
            ensure.assert_called_once_with(profile)
            run.assert_called_once()

    def test_vision_worker_spawn_command_contains_no_api_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp) / "secrets")
            store.set("vision-control:deepseek", "control-secret")
            profile = {
                "id": "deepseek",
                "name": "deepseek",
                "vision": {
                    "enabled": True,
                    "controlCredentialId": "vision-control:deepseek",
                    "proxyPort": 19101,
                },
            }
            with (
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(
                    apiagent,
                    "codex_vision_worker_is_healthy",
                    side_effect=[False, True],
                ),
                patch.object(apiagent, "start_detached_process", return_value=0) as start,
                patch.object(apiagent.time, "sleep"),
            ):
                healthy = apiagent.ensure_codex_vision_worker(profile)

            self.assertTrue(healthy)
            command_text = " ".join(
                [str(start.call_args.args[0]), *start.call_args.args[1]]
            )
            self.assertIn("--vision-worker", command_text)
            self.assertNotIn("control-secret", command_text)
            self.assertNotIn("gemini", command_text.lower())


if __name__ == "__main__":
    unittest.main()
