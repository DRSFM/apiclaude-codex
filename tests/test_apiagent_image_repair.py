from __future__ import annotations

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import apiagent
from codex_history_images import RepairReport
from tests.support import KeychainIsolationMixin


def clean_report(home: Path, dry_run: bool = False) -> RepairReport:
    return RepairReport(codex_home=str(home), dry_run=dry_run)


class ApiAgentImageRepairTests(KeychainIsolationMixin):
    def test_account_repair_branches_before_profile_or_secret_operations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            account_home = root / ".codex"
            account_home.mkdir()
            calls: list[tuple[Path, str, bool]] = []

            def fake_repair(
                home: Path,
                *,
                label: str,
                dry_run: bool = False,
                quiet: bool = False,
            ) -> RepairReport:
                calls.append((home, label, dry_run))
                return clean_report(home, dry_run)

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "load_codex_profiles", side_effect=AssertionError("account repair loaded profiles")),
                patch.object(apiagent, "get_codex_secret", side_effect=AssertionError("account repair read secret")),
                patch.object(apiagent, "write_codex_config", side_effect=AssertionError("account repair wrote config")),
                patch.object(apiagent, "find_codex_desktop_executable", side_effect=AssertionError("account repair found Desktop")),
                patch.object(apiagent, "repair_codex_home_images", side_effect=fake_repair),
            ):
                code = apiagent.codex_main(["--repair-images", "--account", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertEqual(calls, [(account_home.resolve(), "account Codex", True)])

    def test_all_repairs_api_profiles_only_and_never_account_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            account_home = root / ".codex"
            profile_one = {
                "id": "one",
                "name": "one",
                "home": "profiles/one",
            }
            profile_two = {
                "id": "two",
                "name": "two",
                "home": "profiles/two",
            }
            calls: list[Path] = []

            def fake_repair(
                home: Path,
                *,
                label: str,
                dry_run: bool = False,
                quiet: bool = False,
            ) -> RepairReport:
                calls.append(home)
                return clean_report(home, dry_run)

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(
                    apiagent,
                    "load_codex_profiles_for_image_repair",
                    return_value=([profile_one, profile_two], 0),
                ),
                patch.object(apiagent, "repair_codex_home_images", side_effect=fake_repair),
            ):
                code = apiagent.codex_main(["--repair-images", "--all", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertEqual(
                calls,
                [
                    (api_root / "profiles" / "one").resolve(),
                    (api_root / "profiles" / "two").resolve(),
                ],
            )
            self.assertNotIn(account_home.resolve(), calls)

    def test_repair_selector_conflicts_are_rejected_without_writes(self) -> None:
        with patch.object(apiagent, "repair_codex_home_images") as repair:
            code = apiagent.codex_main(["--repair-images", "--all", "--account"])
        self.assertEqual(code, 1)
        repair.assert_not_called()

    def test_unsafe_profile_metadata_cannot_migrate_into_account_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            api_root = root / ".codex-api"
            account_home = root / ".codex"
            account_auth = account_home / "auth.json"
            account_config = account_home / "config.toml"
            account_auth.parent.mkdir(parents=True)
            account_auth.write_text('{"account":"sentinel"}', encoding="utf-8")
            account_config.write_text('model = "sentinel"\n', encoding="utf-8")
            profiles_path = api_root / "profiles.json"
            profiles_path.parent.mkdir(parents=True)
            profiles_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "profiles": [
                            {
                                "id": "bad",
                                "name": "bad",
                                "home": "../.codex",
                                "api_key": "must-not-migrate",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            before_profiles = profiles_path.read_bytes()
            before_auth = account_auth.read_bytes()
            before_config = account_config.read_bytes()
            store = apiagent.SecureStore(root / "secrets")

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", api_root),
                patch.object(apiagent, "CODEX_PROFILES_PATH", profiles_path),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "repair_codex_home_images") as repair,
            ):
                code = apiagent.codex_main(["--repair-images", "--all", "--dry-run"])

            self.assertEqual(code, 1)
            repair.assert_not_called()
            self.assertEqual(profiles_path.read_bytes(), before_profiles)
            self.assertEqual(account_auth.read_bytes(), before_auth)
            self.assertEqual(account_config.read_bytes(), before_config)
            with self.assertRaises(KeyError):
                store.get("codex:bad")

    def test_repair_flags_remain_passthrough_without_repair_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {"id": "relay", "name": "relay", "home": "profiles/relay"}
            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "get_codex_secret", return_value="sk-test"),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "run_command", return_value=0) as run,
            ):
                code = apiagent.codex_main(["--all", "--dry-run"])

            self.assertEqual(code, 0)
            self.assertIn("--all", run.call_args.args[1])
            self.assertIn("--dry-run", run.call_args.args[1])

    def test_account_logon_task_is_explicit_and_does_not_run_repair(self) -> None:
        with (
            patch.object(apiagent.os, "name", "nt"),
            patch.object(apiagent, "load_codex_profiles", side_effect=AssertionError("loaded profiles")),
            patch.object(apiagent, "repair_codex_home_images", side_effect=AssertionError("ran repair")),
            patch.object(apiagent, "run_command", return_value=0) as run,
        ):
            code = apiagent.codex_main(
                ["--repair-images", "--account", "--install-task"]
            )

        self.assertEqual(code, 0)
        command, arguments = run.call_args.args
        self.assertEqual(command, "schtasks")
        self.assertEqual(arguments[:2], ["/Create", "/TN"])
        self.assertIn(apiagent.CODEX_ACCOUNT_IMAGE_REPAIR_TASK, arguments)
        task_command = arguments[arguments.index("/TR") + 1]
        self.assertIn("--repair-images", task_command)
        self.assertIn("--account", task_command)
        self.assertNotIn("--all", task_command)

    def test_account_logon_task_can_be_removed_without_account_access(self) -> None:
        with (
            patch.object(apiagent.os, "name", "nt"),
            patch.object(apiagent, "repair_codex_home_images", side_effect=AssertionError("ran repair")),
            patch.object(apiagent, "run_command", return_value=0) as run,
        ):
            code = apiagent.codex_main(
                ["--repair-images", "--account", "--uninstall-task"]
            )

        self.assertEqual(code, 0)
        run.assert_called_once_with(
            "schtasks",
            ["/Delete", "/TN", apiagent.CODEX_ACCOUNT_IMAGE_REPAIR_TASK, "/F"],
        )

    def test_repair_task_flags_require_explicit_account_opt_in(self) -> None:
        with patch.object(apiagent, "configure_account_image_repair_task") as configure:
            code = apiagent.codex_main(["--repair-images", "--install-task"])

        self.assertEqual(code, 1)
        configure.assert_not_called()

    @unittest.skipUnless(__import__("os").name == "nt", "Windows Desktop hook test")
    def test_desktop_repairs_selected_api_home_before_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "credentialId": "codex:relay",
            }
            store = apiagent.SecureStore(root / "secrets")
            store.set("codex:relay", "sk-test-secret")
            desktop_exe = root / "app" / "ChatGPT.exe"
            calls: list[Path] = []

            def fake_repair(
                home: Path,
                *,
                label: str,
                dry_run: bool = False,
                quiet: bool = False,
            ) -> RepairReport:
                calls.append(home)
                return clean_report(home)

            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "find_codex_desktop_executable", return_value=desktop_exe),
                patch.object(apiagent, "ensure_codex_keyring_auth", return_value=True),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "repair_codex_home_images", side_effect=fake_repair),
                patch.object(apiagent, "start_detached_process", return_value=0) as start,
            ):
                code = apiagent.codex_main(["--desktop", "--api-profile", "relay"])

            self.assertEqual(code, 0)
            self.assertEqual(calls, [(root / ".codex-api" / "profiles" / "relay").resolve()])
            self.assertNotIn(root / ".codex", calls)
            start.assert_called_once()

    @unittest.skipUnless(__import__("os").name == "nt", "Windows Desktop hook test")
    def test_desktop_continues_when_image_repair_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = {
                "id": "relay",
                "name": "relay",
                "home": "profiles/relay",
                "baseUrl": "https://example.test/v1",
                "credentialId": "codex:relay",
            }
            store = apiagent.SecureStore(root / "secrets")
            store.set("codex:relay", "sk-test-secret")
            desktop_exe = root / "app" / "ChatGPT.exe"
            with (
                patch.object(apiagent, "HOME", root),
                patch.object(apiagent, "CODEX_HOME", root / ".codex-api"),
                patch.object(apiagent, "CODEX_DESKTOP_DATA_ROOT", root / "desktop"),
                patch.object(apiagent, "SECRET_STORE", store),
                patch.object(apiagent, "load_codex_profiles", return_value=[profile]),
                patch.object(apiagent, "select_codex_profile", return_value=profile),
                patch.object(apiagent, "find_codex_desktop_executable", return_value=desktop_exe),
                patch.object(apiagent, "ensure_codex_keyring_auth", return_value=True),
                patch.object(apiagent, "add_current_project_trust"),
                patch.object(apiagent, "update_codex_last_used"),
                patch.object(apiagent, "repair_codex_history_images", side_effect=RuntimeError("test repair failure")),
                patch.object(apiagent, "start_detached_process", return_value=0) as start,
            ):
                code = apiagent.codex_main(["--desktop", "--api-profile", "relay"])

            self.assertEqual(code, 0)
            start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
