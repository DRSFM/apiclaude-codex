"""Synthetic credentials only; production keyrings are never enumerated."""
from __future__ import annotations

import base64
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import apiagent
import codex_accounts as accounts
import codex_oauth as oauth


def synthetic_auth(identity="account-a", padding=0):
    def jwt(payload):
        return "eyJhbGciOiJSUzI1NiJ9." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".c3ludGhldGlj"
    token = jwt({"email": identity + "@example.invalid", "exp": 4102444800,
                 "https://api.openai.com/auth": {"chatgpt_account_id": identity,
                                                "chatgpt_user_id": identity, "chatgpt_plan_type": "plus"}})
    return {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": token, "access_token": token,
                       "refresh_token": "synthetic-refresh-" + "x" * padding, "account_id": identity},
            "last_refresh": "2026-09-17T00:00:00Z"}


def refreshed_auth():
    auth = synthetic_auth()
    auth["tokens"]["refresh_token"] = "synthetic-rotated-refresh"
    auth["last_refresh"] = "2026-09-17T01:00:00Z"
    return auth


class FakeKey:
    values = {}

    def __init__(self, home):
        self.home = str(home)

    def read(self):
        return self.values.get(self.home)

    def write(self, value):
        self.values[self.home] = value

    def delete(self):
        self.values.pop(self.home, None)


class OAuthTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.file = self.root / "explicit-export.json"
        self.auth = synthetic_auth()
        self.file.write_text(json.dumps(self.auth), encoding="utf-8")
        self.home = self.root / "named-home"
        self.home.mkdir()
        FakeKey.values = {}
        for target, value in [("WindowsKey", FakeKey), ("encrypt", lambda raw, password: self.encrypt(raw, password, cost=10))]:
            if target == "encrypt":
                self.encrypt = oauth.encrypt
            p = patch.object(oauth, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_verified_export_formats_and_safe_preview(self):
        for value in (self.auth, [self.auth], {"type": "codex", **self.auth["tokens"], "last_refresh": self.auth["last_refresh"]}):
            self.file.write_text(json.dumps(value), encoding="utf-8")
            before = self.file.read_bytes()
            auth, preview, fingerprint = oauth.parse_file(self.file)
            self.assertEqual(auth["tokens"], self.auth["tokens"])
            self.assertTrue(preview["refreshCapable"])
            self.assertFalse(preview["expired"])
            self.assertEqual(len(fingerprint), 64)
            self.assertNotIn(self.auth["tokens"]["access_token"], json.dumps(preview))
            self.assertNotIn("account-a@example.invalid", json.dumps(preview))
            self.assertEqual(before, self.file.read_bytes())

    def test_batch_requires_explicit_selection(self):
        self.file.write_text(json.dumps([self.auth, synthetic_auth("account-b")]))
        with self.assertRaises(oauth.ImportError):
            oauth.parse_file(self.file)
        self.assertEqual(oauth.parse_file(self.file, index=2)[0]["tokens"]["account_id"], "account-b")
        for index in (0, -1, 3):
            with self.assertRaises(oauth.ImportError):
                oauth.parse_file(self.file, index=index)

    def test_missing_refresh_is_explicitly_limited(self):
        del self.auth["tokens"]["refresh_token"]
        self.file.write_text(json.dumps(self.auth))
        auth, preview, _ = oauth.parse_file(self.file)
        self.assertFalse(preview["refreshCapable"])
        self.assertEqual(auth["tokens"]["refresh_token"], "")

    def test_invalid_input_is_safe_and_rejects_identity_mismatch(self):
        cases = ["not-json-synthetic-secret", "[]", "null",
                 json.dumps({"access_token": "synthetic-secret"}),
                 json.dumps({**self.auth, "account_id": "another-account"}),
                 json.dumps({**self.auth, "OPENAI_API_KEY": "synthetic-secret"}),
                 json.dumps({**self.auth, "tokens": {"id_token": "synthetic-secret", "access_token": "synthetic-secret"}})]
        for raw in cases:
            self.file.write_text(raw)
            with self.assertRaises(oauth.ImportError) as ctx:
                oauth.parse_file(self.file)
            self.assertNotIn("synthetic-secret", str(ctx.exception))

    def test_age_chunk_boundaries_and_randomization(self):
        for size in (0, 1, 65535, 65536, 65537, 131072):
            data = b"x" * size
            encrypted = oauth.encrypt(data, "synthetic-password")
            self.assertEqual(oauth.decrypt(encrypted, "synthetic-password"), data)
            self.assertNotEqual(encrypted, oauth.encrypt(data, "synthetic-password"))

    def test_age_rejects_tampering_truncation_and_resource_abuse(self):
        encrypted = oauth.encrypt(b"synthetic plaintext", "synthetic-password")
        for value in (encrypted[:-1], encrypted + b"x", encrypted[:-1] + bytes([encrypted[-1] ^ 1]),
                      encrypted.replace(b" 10\n", b" 99\n"), encrypted.replace(b"-> scrypt", b"-> unknown")):
            with self.assertRaises(oauth.ImportError):
                oauth.decrypt(value, "synthetic-password")
        with self.assertRaises(oauth.ImportError):
            oauth.decrypt(encrypted, "wrong-password")

    def test_save_preserves_other_secrets_and_rolls_back(self):
        with oauth.save_auth(self.home, self.auth):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        key = FakeKey(self.home).read()
        doc = json.loads(oauth.decrypt(path.read_bytes(), key))
        doc["secrets"]["global/OTHER"] = "synthetic-other"
        path.write_bytes(oauth.encrypt(json.dumps(doc).encode(), key))
        original = path.read_bytes()
        with self.assertRaises(RuntimeError):
            with oauth.save_auth(self.home, refreshed_auth()):
                self.assertIn("global/OTHER", json.loads(oauth.decrypt(path.read_bytes(), key))["secrets"])
                raise RuntimeError("registry failure")
        self.assertEqual(original, path.read_bytes())
        self.assertEqual(key, FakeKey(self.home).read())
        backups = list(path.parent.glob("codex_auth.before-import-*.age"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), original)
        for file in path.parent.iterdir():
            self.assertNotIn(self.auth["tokens"]["access_token"].encode(), file.read_bytes())

    def test_new_store_failure_removes_only_new_key_and_file(self):
        with self.assertRaises(RuntimeError):
            with oauth.save_auth(self.home, self.auth):
                raise RuntimeError("recognition failed")
        self.assertIsNone(FakeKey(self.home).read())
        self.assertFalse((self.home / "secrets" / "codex_auth.age").exists())

    def test_missing_optional_dependency_fails_before_writing_auth(self):
        with patch.dict("sys.modules", {"cryptography.hazmat.primitives.ciphers.aead": None}):
            with self.assertRaises(oauth.ImportError):
                with oauth.save_auth(self.home, self.auth):
                    pass
        self.assertIsNone(FakeKey(self.home).read())
        self.assertFalse((self.home / "secrets").exists())

    def test_disk_failure_retains_original_and_key(self):
        with oauth.save_auth(self.home, self.auth):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        original, key = path.read_bytes(), FakeKey(self.home).read()
        with patch.object(oauth, "_atomic", side_effect=OSError("synthetic disk full")):
            with self.assertRaises(OSError):
                with oauth.save_auth(self.home, refreshed_auth()):
                    pass
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(FakeKey(self.home).read(), key)

    def test_unreadable_store_and_missing_shared_key_fail_without_replacement(self):
        directory = self.home / "secrets"
        directory.mkdir()
        path = directory / "codex_auth.age"
        path.write_bytes(b"unreadable-original")
        FakeKey(self.home).write("synthetic-password")
        with self.assertRaises(oauth.ImportError):
            with oauth.save_auth(self.home, self.auth):
                pass
        self.assertEqual(path.read_bytes(), b"unreadable-original")
        path.rename(directory / "mcp_oauth.age")
        FakeKey(self.home).delete()
        with self.assertRaises(oauth.ImportError):
            with oauth.save_auth(self.home, self.auth):
                pass
        self.assertIsNone(FakeKey(self.home).read())

    def test_stale_ambiguous_cross_account_and_refresh_loss_do_not_replace_store(self):
        with oauth.save_auth(self.home, refreshed_auth()):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        original = path.read_bytes()
        ambiguous = refreshed_auth()
        ambiguous["tokens"]["refresh_token"] = "synthetic-other-chain"
        missing = refreshed_auth()
        missing["last_refresh"] = "2026-09-17T02:00:00Z"
        missing["tokens"]["refresh_token"] = ""
        for candidate in (self.auth, ambiguous, missing, synthetic_auth("account-b")):
            with self.subTest(candidate=candidate["last_refresh"]):
                with self.assertRaises(oauth.ImportError):
                    with oauth.save_auth(self.home, candidate):
                        self.fail("unsafe update accepted")
                self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob("*before-import*")))

    def test_identical_import_does_not_rewrite_or_advance_refresh_time(self):
        with oauth.save_auth(self.home, self.auth):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        original = path.read_bytes()
        candidate = synthetic_auth()
        candidate["last_refresh"] = "2099-01-01T00:00:00Z"
        with oauth.save_auth(self.home, candidate):
            pass
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(path.parent.glob("*before-import*")))

    def test_forward_update_is_retained_on_restart_and_old_export_is_rejected(self):
        with oauth.save_auth(self.home, self.auth):
            pass
        with oauth.save_auth(self.home, refreshed_auth()):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        document = json.loads(oauth.decrypt(path.read_bytes(), FakeKey(self.home).read()))
        self.assertEqual(json.loads(document["secrets"]["global/CODEX_AUTH"]), refreshed_auth())
        with self.assertRaises(oauth.ImportError):
            with oauth.save_auth(self.home, self.auth):
                pass

    def test_token_issue_time_can_order_exports_but_expiry_alone_cannot(self):
        def with_claims(auth, **updates):
            parts = auth["tokens"]["access_token"].split('.')
            claims = oauth._jwt(auth["tokens"]["access_token"])
            claims.update(updates)
            parts[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
            auth["tokens"]["access_token"] = '.'.join(parts)
            auth.pop("last_refresh", None)
            return auth
        old = with_claims(synthetic_auth(), iat=1750000000, exp=1750010000)
        newer = with_claims(refreshed_auth(), iat=1750001000, exp=1750011000)
        self.assertTrue(oauth.check_update(old, newer))
        for candidate in (
            with_claims(refreshed_auth(), iat=1750000000, exp=1750011000),
            with_claims(refreshed_auth(), iat=1750001000, exp=1750009000),
            with_claims(refreshed_auth(), iat=None, exp=1750011000),
        ):
            with self.assertRaises(oauth.ImportError):
                oauth.check_update(old, candidate)

    def test_update_cannot_cross_user_identity_or_use_future_refresh_timestamp(self):
        changed_user = refreshed_auth()
        parts = changed_user["tokens"]["id_token"].split('.')
        claims = oauth._jwt(changed_user["tokens"]["id_token"])
        claims["https://api.openai.com/auth"]["chatgpt_user_id"] = "other-user"
        parts[1] = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
        changed_user["tokens"]["id_token"] = '.'.join(parts)
        future = refreshed_auth()
        future["last_refresh"] = "2099-01-01T00:00:00Z"
        for candidate in (changed_user, future):
            with self.assertRaises(oauth.ImportError):
                oauth.check_update(self.auth, candidate)

    def test_external_change_during_encryption_is_not_overwritten(self):
        with oauth.save_auth(self.home, self.auth):
            pass
        path = self.home / "secrets" / "codex_auth.age"
        changed = b"synthetic-concurrent-official-ciphertext"
        def concurrent_encrypt(raw, password):
            path.write_bytes(changed)
            return self.encrypt(raw, password, cost=10)
        with patch.object(oauth, "encrypt", side_effect=concurrent_encrypt):
            with self.assertRaisesRegex(oauth.ImportError, "changed during import"):
                with oauth.save_auth(self.home, refreshed_auth()):
                    pass
        self.assertEqual(path.read_bytes(), changed)

    def test_failed_verification_never_rolls_back_over_official_refresh(self):
        # Cover both initial import and update: the new key must survive when
        # the official client has saved newer ciphertext using that key.
        for initial in (True, False):
            home = self.root / ("initial" if initial else "updated")
            home.mkdir()
            if not initial:
                with oauth.save_auth(home, self.auth):
                    pass
            path = home / "secrets" / "codex_auth.age"
            with self.assertRaisesRegex(oauth.ImportError, "latest store retained"):
                with oauth.save_auth(home, refreshed_auth()):
                    password = FakeKey(home).read()
                    document = json.loads(oauth.decrypt(path.read_bytes(), password))
                    document["secrets"]["global/CODEX_AUTH"] = json.dumps({
                        **refreshed_auth(), "last_refresh": "2026-09-17T02:00:00Z"})
                    latest = oauth.encrypt(json.dumps(document).encode(), password)
                    path.write_bytes(latest)
                    raise RuntimeError("registry persistence failed")
            self.assertEqual(path.read_bytes(), latest)
            self.assertEqual(FakeKey(home).read(), password)

    def test_import_preview_reuse_explicit_update_and_failed_recognition(self):
        for key, value in {"HOME": self.root, "CODEX_HOME": self.root / ".codex-api",
                           "CODEX_PROFILES_PATH": self.root / ".codex-api" / "profiles.json",
                           "CODEX_DESKTOP_DATA_ROOT": self.root / ".desktop"}.items():
            p = patch.object(apiagent, key, value)
            p.start()
            self.addCleanup(p.stop)
        source = self.file.read_bytes()
        output = io.StringIO()
        args = ["import", "work", "--file", str(self.file)]
        with redirect_stdout(output), redirect_stderr(output), patch.object(accounts, "profile_busy", return_value=False), patch.object(apiagent, "find_codex_cli_executable", return_value="codex.exe"), patch.object(apiagent, "ensure_private_desktop_directory"), patch.object(accounts, "read_account", return_value={"type": "chatgpt", "email": "account-a@example.invalid"}) as read:
            self.assertEqual(accounts.main([*args, "--dry-run"], apiagent), 0)
            self.assertFalse(apiagent.CODEX_HOME.exists())
            self.assertEqual(accounts.main(args, apiagent), 0)
            profile = apiagent.load_codex_profiles()[0]
            stored = apiagent.codex_profile_home(profile) / "secrets" / "codex_auth.age"
            before = stored.read_bytes()
            self.assertEqual(accounts.main(args, apiagent), 0)
            self.assertEqual(accounts.main(["import", "duplicate", "--file", str(self.file)], apiagent), 0)
            self.assertEqual(len(apiagent.load_codex_profiles()), 1)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(stored.read_bytes(), before)
            read.return_value = None
            self.assertEqual(accounts.main([*args, "--update"], apiagent), 1)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(apiagent.load_codex_profiles(), [profile])
            read.return_value = {"type": "chatgpt", "email": "account-a@example.invalid"}
            save = apiagent.save_codex_profiles
            calls = []
            def fail_once(profiles):
                calls.append(True)
                if len(calls) == 1:
                    raise OSError("synthetic registry failure")
                return save(profiles)
            with patch.object(apiagent, "save_codex_profiles", side_effect=fail_once):
                self.assertEqual(accounts.main([*args, "--update"], apiagent), 1)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(apiagent.load_codex_profiles(), [profile])
            with patch.object(accounts, "profile_busy", return_value=True):
                self.assertEqual(accounts.main([*args, "--update"], apiagent), 1)
            self.assertEqual(stored.read_bytes(), before)
            # The official on-disk store, not importer metadata, determines
            # which credentials are current after a refresh/restart.
            self.file.write_text(json.dumps(refreshed_auth()))
            self.assertEqual(accounts.main([*args, "--update"], apiagent), 0)
            current = stored.read_bytes()
            self.assertNotEqual(current, before)
            self.file.write_bytes(source)
            read.reset_mock()
            self.assertEqual(accounts.main([*args, "--update"], apiagent), 1)
            read.assert_not_called()
            self.assertEqual(stored.read_bytes(), current)
            # A prior logout leaves registry identity metadata behind. Duplicate
            # import must not silently claim to have restored authentication.
            stored.unlink()
            read.reset_mock()
            read.return_value = None
            output.truncate(0)
            output.seek(0)
            self.assertEqual(accounts.main(args, apiagent), 0)
            self.assertEqual(accounts.main(["import", "duplicate", "--file", str(self.file)], apiagent), 0)
            read.assert_not_called()
            self.assertFalse(stored.exists())
            self.assertIn('login was not checked or restored', output.getvalue())
            self.assertIn('--update', output.getvalue())
        self.assertEqual(self.file.read_bytes(), source)
        self.assertNotIn(self.auth["tokens"]["access_token"], output.getvalue())
        self.assertFalse((self.root / ".codex").exists())


@unittest.skipUnless(os.name == "nt" and os.environ.get("APICODEX_TEST_NATIVE_OAUTH"), "explicit isolated native auth test")
class NativeOAuthTests(unittest.TestCase):
    def test_official_shared_nested_features(self):
        import codex_account_resources as resources
        cli = os.environ["APICODEX_TEST_NATIVE_OAUTH"]
        with tempfile.TemporaryDirectory(prefix="apicodex-native-features-") as tmp:
            home = Path(tmp)
            target = 'cli_auth_credentials_store = "keyring"\nforced_login_method = "chatgpt"\n'
            for source in (
                '[features.context_management]\nexperimental_mode = true\n',
                'features.context_management.experimental_mode = true\n',
                'features = { context_management = { experimental_mode = true }, secret_auth_storage = false }\n',
            ):
                with self.subTest(source=source):
                    (home / 'config.toml').write_text(resources.merge_config(target, source), encoding='utf-8')
                    self.assertIsNone(accounts.read_account(home, cli))
            self.assertFalse((home / 'auth.json').exists())

    def test_official_two_accounts_restart_and_independent_logout(self):
        # Set APICODEX_TEST_NATIVE_OAUTH to the official executable. Only fake
        # tokens and fresh temporary homes are used; no network request/refresh.
        cli = os.environ["APICODEX_TEST_NATIVE_OAUTH"]
        with tempfile.TemporaryDirectory(prefix="apicodex-native-auth-") as tmp:
            homes = [Path(tmp) / name for name in ("a", "b")]
            keys = []
            try:
                for i, home in enumerate(homes):
                    home.mkdir()
                    (home / "config.toml").write_text('cli_auth_credentials_store = "keyring"\nforced_login_method = "chatgpt"\n')
                    key = oauth.WindowsKey(home)
                    self.assertIsNone(key.read())
                    keys.append(key)
                    with oauth.save_auth(home, synthetic_auth("account-" + str(i), padding=7000)):
                        self.assertEqual(accounts.read_account(home, cli)["email"], f"account-{i}@example.invalid")
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(lambda h: accounts.read_account(h, cli), homes))
                self.assertEqual([r["email"] for r in results], ["account-0@example.invalid", "account-1@example.invalid"])
                second = (homes[1] / "secrets" / "codex_auth.age").read_bytes()
                result = subprocess.run([cli, "logout"], env=accounts.clean_environment(homes[0]), capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0)
                native = json.loads(oauth.decrypt((homes[0] / "secrets" / "codex_auth.age").read_bytes(), keys[0].read()))
                self.assertNotIn("global/CODEX_AUTH", native["secrets"])
                self.assertIsNone(accounts.read_account(homes[0], cli))
                self.assertEqual(accounts.read_account(homes[1], cli)["email"], "account-1@example.invalid")
                self.assertEqual((homes[1] / "secrets" / "codex_auth.age").read_bytes(), second)
                for home in homes:
                    self.assertFalse((home / "auth.json").exists())
            finally:
                for key in keys:
                    key.delete()
                    self.assertIsNone(key.read())


if __name__ == "__main__":
    unittest.main()
