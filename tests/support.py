"""Shared helpers that keep the test suite off the real credential stores."""

from __future__ import annotations

import sys
import unittest
from unittest.mock import patch

import secure_store


class _FakeKeychain:
    """In-memory stand-in for the macOS login keychain.

    Mirrors the observable behaviour of the Security framework helpers in
    ``secure_store`` so tests exercise the same ``SecureStore`` code paths
    without writing generic passwords into the developer's real keychain.
    """

    def __init__(self) -> None:
        self.items: dict[tuple[bytes, str], str] = {}

    def set(self, service: bytes, credential_id: str, secret: str) -> None:
        self.items[(service, credential_id)] = secret

    def get(self, service: bytes, credential_id: str) -> str:
        try:
            return self.items[(service, credential_id)]
        except KeyError:
            raise KeyError(f"Credential '{credential_id}' is not stored") from None

    def clear(self, service: bytes, credential_id: str) -> None:
        self.items.pop((service, credential_id), None)


class KeychainIsolationMixin(unittest.TestCase):
    """Route ``SecureStore`` through an in-memory keychain on macOS.

    The macOS backend derives its keychain service name from the store root, so
    tests backed by throwaway temporary directories would otherwise leave one
    unreachable generic password behind per run. Tests that assert on the real
    Security framework calls should not use this mixin.
    """

    def setUp(self) -> None:
        super().setUp()
        self.fake_keychain = _FakeKeychain()
        if sys.platform != "darwin":
            return
        for name, target in (
            ("_macos_keychain_set", self.fake_keychain.set),
            ("_macos_keychain_get", self.fake_keychain.get),
            ("_macos_keychain_clear", self.fake_keychain.clear),
        ):
            patcher = patch.object(secure_store, name, target)
            patcher.start()
            self.addCleanup(patcher.stop)
