from __future__ import annotations

import ctypes
import hashlib
import os
import secrets
import sys
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


CRYPTPROTECT_UI_FORBIDDEN = 0x01
CLEARED_MARKER = b"APIAGENT-CLEARED-V1\x00"
MACOS_KEYCHAIN_SERVICE = b"apiagent.credentials.v1"
ERR_SEC_DUPLICATE_ITEM = -25299
ERR_SEC_ITEM_NOT_FOUND = -25300


class SecureStoreError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def _macos_frameworks() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    try:
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError as exc:
        raise SecureStoreError(
            "macOS Keychain frameworks are unavailable"
        ) from exc

    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemDelete.argtypes = [ctypes.c_void_p]
    security.SecKeychainItemDelete.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    return security, core_foundation


def _macos_keychain_error(action: str, status: int) -> SecureStoreError:
    return SecureStoreError(f"macOS Keychain {action} failed with status {status}")


def _macos_keychain_service(root: Path) -> bytes:
    resolved = root.expanduser().resolve(strict=False)
    production = (Path.home() / ".apiagent-secrets").resolve(strict=False)
    if resolved == production:
        return MACOS_KEYCHAIN_SERVICE
    digest = hashlib.sha256(os.fsencode(resolved)).hexdigest()[:24].encode("ascii")
    return MACOS_KEYCHAIN_SERVICE + b"." + digest


def _macos_keychain_set(service: bytes, credential_id: str, secret: str) -> None:
    security, core_foundation = _macos_frameworks()
    account = credential_id.encode("utf-8")
    secret_bytes = secret.encode("utf-8")
    secret_buffer = ctypes.create_string_buffer(secret_bytes, len(secret_bytes))
    secret_pointer = ctypes.cast(secret_buffer, ctypes.c_void_p)
    item = ctypes.c_void_p()

    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        None,
        None,
        ctypes.byref(item),
    )
    if status == 0:
        try:
            status = security.SecKeychainItemModifyAttributesAndData(
                item,
                None,
                len(secret_bytes),
                secret_pointer,
            )
        finally:
            core_foundation.CFRelease(item)
        if status != 0:
            raise _macos_keychain_error("update", status)
        return
    if status != ERR_SEC_ITEM_NOT_FOUND:
        raise _macos_keychain_error("lookup", status)

    status = security.SecKeychainAddGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        len(secret_bytes),
        secret_pointer,
        ctypes.byref(item),
    )
    if item.value:
        core_foundation.CFRelease(item)
    if status == ERR_SEC_DUPLICATE_ITEM:
        # Another process may have inserted the same credential after lookup.
        _macos_keychain_set(service, credential_id, secret)
        return
    if status != 0:
        raise _macos_keychain_error("write", status)


def _macos_keychain_get(service: bytes, credential_id: str) -> str:
    security, core_foundation = _macos_frameworks()
    account = credential_id.encode("utf-8")
    password_length = ctypes.c_uint32()
    password_data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        ctypes.byref(password_length),
        ctypes.byref(password_data),
        ctypes.byref(item),
    )
    if status == ERR_SEC_ITEM_NOT_FOUND:
        raise KeyError(f"Credential '{credential_id}' is not stored")
    if status != 0:
        raise _macos_keychain_error("read", status)

    try:
        value = ctypes.string_at(password_data, password_length.value)
    finally:
        if password_data.value:
            security.SecKeychainItemFreeContent(None, password_data)
        if item.value:
            core_foundation.CFRelease(item)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecureStoreError(
            f"Credential '{credential_id}' contains invalid data"
        ) from exc


def _macos_keychain_clear(service: bytes, credential_id: str) -> None:
    security, core_foundation = _macos_frameworks()
    account = credential_id.encode("utf-8")
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service),
        service,
        len(account),
        account,
        None,
        None,
        ctypes.byref(item),
    )
    if status == ERR_SEC_ITEM_NOT_FOUND:
        return
    if status != 0:
        raise _macos_keychain_error("lookup", status)
    try:
        status = security.SecKeychainItemDelete(item)
    finally:
        core_foundation.CFRelease(item)
    if status != 0:
        raise _macos_keychain_error("delete", status)


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
            _macos_keychain_set(
                _macos_keychain_service(self.root), credential_id, secret
            )
            if self.get(credential_id) != secret:
                raise SecureStoreError(
                    f"Failed to verify stored credential '{credential_id}'"
                )
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
            return _macos_keychain_get(
                _macos_keychain_service(self.root), credential_id
            )

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
            _macos_keychain_clear(
                _macos_keychain_service(self.root), credential_id
            )
            return

        path = self._path(credential_id)
        if path.exists():
            size = max(path.stat().st_size, 64)
            path.write_bytes(CLEARED_MARKER + os.urandom(size - len(CLEARED_MARKER)))
