from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import secure_store
from secure_store import SecureStore


class SecureStoreValidationTests(unittest.TestCase):
    def test_rejects_empty_credential_id_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))

            with self.assertRaises(ValueError):
                store.set("", "secret")
            with self.assertRaises(ValueError):
                store.set("claude:test", "")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class WindowsSecureStoreTests(unittest.TestCase):
    def test_round_trip_uses_encrypted_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            credential_id = "claude:test-node"
            secret = "sk-test-secret-value"

            store.set(credential_id, secret)

            stored_bytes = next(Path(tmp).glob("*.bin")).read_bytes()
            self.assertNotIn(secret.encode("utf-8"), stored_bytes)
            self.assertEqual(store.get(credential_id), secret)

    def test_different_credentials_use_different_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:one", "secret-one")
            store.set("codex:one", "secret-two")

            self.assertEqual(len(list(Path(tmp).glob("*.bin"))), 2)
            self.assertEqual(store.get("claude:one"), "secret-one")
            self.assertEqual(store.get("codex:one"), "secret-two")

    def test_clear_makes_secret_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))
            store.set("claude:removed", "secret")

            store.clear("claude:removed")

            with self.assertRaises(KeyError):
                store.get("claude:removed")


@unittest.skipUnless(sys.platform == "darwin", "macOS Keychain test")
class MacOSSecureStoreTests(unittest.TestCase):
    def test_production_and_temporary_roots_use_separate_services(self) -> None:
        production = secure_store._macos_keychain_service(
            Path.home() / ".apiagent-secrets"
        )
        temporary = secure_store._macos_keychain_service(
            Path(tempfile.gettempdir()) / "apiagent-test-secrets"
        )

        self.assertEqual(production, secure_store.MACOS_KEYCHAIN_SERVICE)
        self.assertNotEqual(temporary, production)
        self.assertTrue(temporary.startswith(production + b"."))

    def test_round_trip_routes_through_keychain_without_creating_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "secrets"
            store = SecureStore(root)
            credential_id = "claude:test-node"
            secret = "sk-test-secret-value"

            with (
                patch.object(secure_store, "_macos_keychain_set") as set_secret,
                patch.object(
                    secure_store,
                    "_macos_keychain_get",
                    return_value=secret,
                ) as get_secret,
            ):
                store.set(credential_id, secret)
                self.assertEqual(store.get(credential_id), secret)

            service = secure_store._macos_keychain_service(root)
            set_secret.assert_called_once_with(service, credential_id, secret)
            self.assertEqual(get_secret.call_count, 2)
            self.assertFalse(root.exists())

    def test_clear_routes_through_keychain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))

            with patch.object(secure_store, "_macos_keychain_clear") as clear_secret:
                store.clear("claude:removed")

            clear_secret.assert_called_once_with(
                secure_store._macos_keychain_service(Path(tmp)),
                "claude:removed",
            )


@unittest.skipUnless(sys.platform == "darwin", "macOS Keychain test")
class MacOSKeychainBackendTests(unittest.TestCase):
    """Exercise the real Security framework calls, cleaning up after itself.

    Every other suite routes ``SecureStore`` through the in-memory fake in
    ``tests.support``, so this is the only place the live keychain backend is
    covered. The cleanup hook runs even when an assertion fails, which keeps
    throwaway generic passwords out of the developer's login keychain.
    """

    def test_real_keychain_round_trip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp) / "secrets")
            credential_id = f"apiagent-test:{uuid.uuid4().hex}"
            secret = "sk-live-keychain-round-trip"
            self.addCleanup(store.clear, credential_id)

            store.set(credential_id, secret)
            self.assertEqual(store.get(credential_id), secret)

            store.set(credential_id, "sk-live-keychain-updated")
            self.assertEqual(store.get(credential_id), "sk-live-keychain-updated")

            store.clear(credential_id)
            with self.assertRaises(KeyError):
                store.get(credential_id)
            self.assertFalse((Path(tmp) / "secrets").exists())

    def test_clear_is_idempotent_for_unknown_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SecureStore(Path(tmp))

            store.clear(f"apiagent-test:{uuid.uuid4().hex}")


if __name__ == "__main__":
    unittest.main()
