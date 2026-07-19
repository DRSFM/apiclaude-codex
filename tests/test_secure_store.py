from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from secure_store import SecureStore


@unittest.skipUnless(__import__("os").name == "nt", "Windows DPAPI test")
class SecureStoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
