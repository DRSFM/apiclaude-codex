from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x01
CLEARED_MARKER = b"APIAGENT-CLEARED-V1\x00"
MACOS_KEYCHAIN_SERVICE = b"apiagent.credentials.v2"
MACOS_LEGACY_KEYCHAIN_SERVICE = b"apiagent.credentials.v1"
MACOS_KEYCHAIN_TOOL = "/usr/bin/security"


class SecureStoreError(RuntimeError):
    pass


def _macos_keychain_service(root: Path) -> bytes:
    resolved = root.expanduser().resolve(strict=False)
    production = (Path.home() / ".apiagent-secrets").resolve(strict=False)
    if resolved == production:
        return MACOS_KEYCHAIN_SERVICE
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:24].encode("ascii")
    return MACOS_KEYCHAIN_SERVICE + b"." + digest


def _macos_legacy_keychain_service(root: Path) -> bytes:
    service = _macos_keychain_service(root)
    return service.replace(
        MACOS_KEYCHAIN_SERVICE, MACOS_LEGACY_KEYCHAIN_SERVICE, 1
    )


def _macos_security_tool_get(service: bytes, credential_id: str) -> str:
    completed = subprocess.run(
        [
            MACOS_KEYCHAIN_TOOL,
            "find-generic-password",
            "-a",
            credential_id,
            "-s",
            service.decode("ascii"),
            "-w",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        if completed.returncode == 44 or "could not be found" in completed.stderr:
            raise KeyError(f"Credential '{credential_id}' is not stored")
        raise SecureStoreError(
            f"macOS Keychain read failed with status {completed.returncode}"
        )
    return completed.stdout.rstrip("\n")


def _macos_security_tool_set(
    service: bytes, credential_id: str, secret: str
) -> None:
    lookup = subprocess.run(
        [
            MACOS_KEYCHAIN_TOOL,
            "find-generic-password",
            "-a",
            credential_id,
            "-s",
            service.decode("ascii"),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    exists = lookup.returncode == 0
    missing = lookup.returncode == 44 or "could not be found" in lookup.stderr
    if not exists and not missing:
        raise SecureStoreError(
            f"macOS Keychain lookup failed with status {lookup.returncode}"
        )
    command = [
        MACOS_KEYCHAIN_TOOL,
        "add-generic-password",
        "-a",
        credential_id,
        "-s",
        service.decode("ascii"),
    ]
    if exists:
        command.append("-U")
    else:
        command.append("-A")
    command.append("-w")
    completed = subprocess.run(
        command,
        input=f"{secret}\n{secret}\n",
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SecureStoreError(
            f"macOS Keychain write failed with status {completed.returncode}"
        )


def _macos_security_tool_clear(service: bytes, credential_id: str) -> None:
    completed = subprocess.run(
        [
            MACOS_KEYCHAIN_TOOL,
            "delete-generic-password",
            "-a",
            credential_id,
            "-s",
            service.decode("ascii"),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    if (
        completed.returncode not in (0, 44)
        and "could not be found" not in completed.stderr
    ):
        raise SecureStoreError(
            f"macOS Keychain delete failed with status {completed.returncode}"
        )


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DataBlob(
            len(data),
            ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
        ),
        buffer,
    )


def _protect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise SecureStoreError(
            "Secure credential storage is not supported on this platform"
        )

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = _DataBlob()
    _ = (input_buffer, entropy_buffer)

    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "API Agent credential",
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise SecureStoreError(
            f"Windows DPAPI encryption failed with error {ctypes.get_last_error()}"
        )

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _unprotect(data: bytes, entropy: bytes) -> bytes:
    if os.name != "nt":
        raise SecureStoreError(
            "Secure credential storage is not supported on this platform"
        )

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(entropy)
    output_blob = _DataBlob()
    _ = (input_buffer, entropy_buffer)

    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    ):
        raise SecureStoreError(
            f"Windows DPAPI decryption failed with error {ctypes.get_last_error()}"
        )

    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


class SecureStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, str] = {}

    @staticmethod
    def _entropy(credential_id: str) -> bytes:
        return f"apiagent:v1:{credential_id}".encode("utf-8")

    def _path(self, credential_id: str) -> Path:
        digest = hashlib.sha256(credential_id.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.bin"

    def set(self, credential_id: str, secret: str) -> None:
        if not credential_id:
            raise ValueError("credential_id cannot be empty")
        if not secret:
            raise ValueError("secret cannot be empty")

        if sys.platform == "darwin":
            service = _macos_keychain_service(self.root)
            legacy_service = _macos_legacy_keychain_service(self.root)
            _macos_security_tool_set(
                service, credential_id, secret
            )
            if _macos_security_tool_get(service, credential_id) != secret:
                raise SecureStoreError(
                    f"Failed to verify stored credential '{credential_id}'"
                )
            # A successfully verified v2 value is authoritative. Removing the
            # interpreter-bound v1 copy prevents a later read from seeing two
            # credentials that may diverge after an update.
            _macos_security_tool_clear(legacy_service, credential_id)
            self._cache[credential_id] = secret
            return

        encrypted = _protect(secret.encode("utf-8"), self._entropy(credential_id))
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(credential_id)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_bytes(encrypted)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        if self.get(credential_id) != secret:
            raise SecureStoreError(
                f"Failed to verify stored credential '{credential_id}'"
            )

    def get(self, credential_id: str) -> str:
        if sys.platform == "darwin":
            cached = self._cache.get(credential_id)
            if cached is not None:
                return cached
            service = _macos_keychain_service(self.root)
            legacy_service = _macos_legacy_keychain_service(self.root)
            try:
                value = _macos_security_tool_get(service, credential_id)
            except KeyError:
                value = _macos_security_tool_get(legacy_service, credential_id)
                _macos_security_tool_set(service, credential_id, value)
                if _macos_security_tool_get(service, credential_id) != value:
                    raise SecureStoreError(
                        f"Failed to verify migrated credential '{credential_id}'"
                    )
                _macos_security_tool_clear(legacy_service, credential_id)
            else:
                try:
                    legacy_value = _macos_security_tool_get(
                        legacy_service, credential_id
                    )
                except KeyError:
                    pass
                else:
                    if legacy_value != value:
                        raise SecureStoreError(
                            f"Conflicting macOS Keychain credentials for "
                            f"'{credential_id}'; re-save the API credential"
                        )
                    _macos_security_tool_clear(legacy_service, credential_id)
            self._cache[credential_id] = value
            return value

        path = self._path(credential_id)
        if not path.exists():
            raise KeyError(f"Credential '{credential_id}' is not stored")
        encrypted = path.read_bytes()
        if encrypted.startswith(CLEARED_MARKER):
            raise KeyError(f"Credential '{credential_id}' is not stored")
        try:
            return _unprotect(
                encrypted,
                self._entropy(credential_id),
            ).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecureStoreError(
                f"Credential '{credential_id}' contains invalid data"
            ) from exc

    def clear(self, credential_id: str) -> None:
        if sys.platform == "darwin":
            self._cache.pop(credential_id, None)
            _macos_security_tool_clear(
                _macos_keychain_service(self.root), credential_id
            )
            _macos_security_tool_clear(
                _macos_legacy_keychain_service(self.root), credential_id
            )
            return

        path = self._path(credential_id)
        if path.exists():
            size = max(path.stat().st_size, 64)
            path.write_bytes(CLEARED_MARKER + os.urandom(size - len(CLEARED_MARKER)))
