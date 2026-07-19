from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
from ctypes import wintypes
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x01
CLEARED_MARKER = b"APIAGENT-CLEARED-V1\x00"


class SecureStoreError(RuntimeError):
    pass


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
        raise SecureStoreError("Secure credential storage currently requires Windows DPAPI")

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
        raise SecureStoreError("Secure credential storage currently requires Windows DPAPI")

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
        path = self._path(credential_id)
        if path.exists():
            size = max(path.stat().st_size, 64)
            path.write_bytes(CLEARED_MARKER + os.urandom(size - len(CLEARED_MARKER)))
