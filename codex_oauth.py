"""Explicit OAuth import into the official Windows Codex secrets store.

Only this optional import path requires cryptography. Cryptographic primitives
come from that library; the bounded age v1 scrypt envelope follows C2SP/age.
No default-account discovery, token requests, or plaintext persistence occurs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class ImportError(ValueError):
    """Safe errors: never include input values, decoded JSON, or native errors."""


MAX_BYTES = 2 * 1024 * 1024


def _crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        from cryptography.hazmat.primitives import hashes
    except ModuleNotFoundError:
        raise ImportError("OAuth import requires the optional package: python -m pip install cryptography") from None
    return ChaCha20Poly1305, Scrypt, HKDF, hashes


def _b64(value: bytes) -> bytes:
    return base64.b64encode(value).rstrip(b"=")


def _unb64(value: bytes, size: int) -> bytes:
    raw = base64.b64decode(value + b"=" * (-len(value) % 4), validate=True)
    if len(raw) != size or _b64(raw) != value:
        raise ValueError()
    return raw


def _keys(password: str, salt: bytes, cost: int):
    ChaCha, Scrypt, HKDF, hashes = _crypto()
    # Bound memory/CPU before processing a possibly corrupted local file.
    if not 1 <= cost <= 20:
        raise ImportError("Unsupported encrypted-store work factor; use official login.")
    wrap = Scrypt(salt=b"age-encryption.org/v1/scrypt" + salt, length=32,
                  n=1 << cost, r=8, p=1).derive(password.encode("utf-8"))
    def derive(key: bytes, info: bytes, nonce: bytes = b"") -> bytes:
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=nonce, info=info).derive(key)
    return ChaCha, wrap, derive


def encrypt(plaintext: bytes, password: str, *, cost: int = 18) -> bytes:
    if len(plaintext) > MAX_BYTES:
        raise ImportError("Authentication store is too large.")
    salt, key, nonce = os.urandom(16), os.urandom(16), os.urandom(16)
    ChaCha, wrap, derive = _keys(password, salt, cost)
    body = ChaCha(wrap).encrypt(bytes(12), key, b"")
    header = b"age-encryption.org/v1\n-> scrypt " + _b64(salt) + b" " + str(cost).encode() + b"\n" + _b64(body) + b"\n---"
    mac = hmac.digest(derive(key, b"header"), header, "sha256")
    cipher = ChaCha(derive(key, b"payload", nonce))
    chunks = [plaintext[i:i + 65536] for i in range(0, len(plaintext), 65536)] or [b""]
    payload = b"".join(cipher.encrypt(i.to_bytes(11, "big") + bytes([i == len(chunks) - 1]), chunk, b"")
                       for i, chunk in enumerate(chunks))
    return header + b" " + _b64(mac) + b"\n" + nonce + payload


def decrypt(ciphertext: bytes, password: str) -> bytes:
    _crypto()
    try:
        if len(ciphertext) > MAX_BYTES + 4096:
            raise ValueError()
        lines = ciphertext.split(b"\n", 4)
        if len(lines) != 5 or lines[0] != b"age-encryption.org/v1":
            raise ValueError()
        match = re.fullmatch(rb"-> scrypt ([A-Za-z0-9+/]{22}) ([1-9][0-9]?)", lines[1])
        if not match or not lines[3].startswith(b"--- "):
            raise ValueError()
        salt = _unb64(match[1], 16)
        body, mac = _unb64(lines[2], 32), _unb64(lines[3][4:], 32)
        ChaCha, wrap, derive = _keys(password, salt, int(match[2]))
        key = ChaCha(wrap).decrypt(bytes(12), body, b"")
        header = b"\n".join(lines[:3]) + b"\n---"
        if not hmac.compare_digest(mac, hmac.digest(derive(key, b"header"), header, "sha256")):
            raise ValueError()
        nonce, payload = lines[4][:16], lines[4][16:]
        if len(nonce) != 16 or len(payload) < 16:
            raise ValueError()
        cipher = ChaCha(derive(key, b"payload", nonce))
        result = []
        for i, start in enumerate(range(0, len(payload), 65552)):
            chunk = payload[start:start + 65552]
            final = start + len(chunk) == len(payload)
            if len(chunk) < 16 or (i and len(chunk) == 16):
                raise ValueError()
            result.append(cipher.decrypt(i.to_bytes(11, "big") + bytes([final]), chunk, b""))
        return b"".join(result)
    except ImportError:
        raise
    except Exception:
        raise ImportError("Could not decrypt/verify the official authentication store; nothing was replaced.") from None


def _clean(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.lstrip("\ufeff\u200b\u200c\u200d\u2060").strip()


def _jwt(value: str) -> dict[str, Any]:
    try:
        parts = value.split(".")
        if len(parts) != 3 or not all(parts):
            raise ValueError()
        data = json.loads(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True))
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except Exception:
        raise ImportError("OAuth file contains an invalid JWT structure.") from None


def account_email(auth: dict[str, Any]) -> str:
    identity = _jwt(auth["tokens"]["id_token"])
    email = _clean(identity.get("email"))
    if not email:
        claim = identity.get("https://api.openai.com/profile")
        email = _clean(claim.get("email")) if isinstance(claim, dict) else ""
    return email if "@" in email and not any(ord(c) < 32 for c in email) else ""


def parse_file(path: Path, *, index: int | None = None) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return official auth, safe unverified preview, and a local identity hash."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ImportError("Could not read a valid OAuth JSON file (maximum 2 MiB).") from None
    if isinstance(value, list):
        if index is None and len(value) != 1:
            raise ImportError("Multiple accounts found; select one with --index N (starting at 1).")
        chosen = (1 if index is None else index) - 1
        if chosen < 0 or chosen >= len(value):
            raise ImportError("Account index is outside the file's account list.")
        value = value[chosen]
    elif index is not None:
        raise ImportError("--index applies only to an account array.")
    if not isinstance(value, dict) or value.get("auth_mode") not in (None, "chatgpt") or value.get("OPENAI_API_KEY") or value.get("openai_api_key"):
        raise ImportError("Select a ChatGPT OAuth export, not an API key or agent identity export.")
    tokens = value.get("tokens", value)
    if not isinstance(tokens, dict):
        raise ImportError("Unsupported OAuth export structure.")
    identity_token, access, refresh = (_clean(tokens.get(k)) for k in ("id_token", "access_token", "refresh_token"))
    if not identity_token or not access:
        raise ImportError("Import requires id_token and access_token; access-only exports cannot provide managed ChatGPT login. Use official login.")
    identity, claims = _jwt(identity_token), _jwt(access)
    auth_claim = identity.get("https://api.openai.com/auth") or {}
    access_claim = claims.get("https://api.openai.com/auth") or {}
    if not isinstance(auth_claim, dict) or not isinstance(access_claim, dict):
        raise ImportError("Invalid OAuth identity claims.")
    ids = {_clean(x) for x in (tokens.get("account_id"), value.get("account_id"),
           auth_claim.get("chatgpt_account_id"), access_claim.get("chatgpt_account_id")) if _clean(x)}
    if len(ids) != 1:
        raise ImportError("OAuth account identity is missing or inconsistent.")
    account_id = ids.pop()
    email = _clean(identity.get("email"))
    if not email:
        profile_claim = identity.get("https://api.openai.com/profile")
        email = _clean(profile_claim.get("email")) if isinstance(profile_claim, dict) else ""
    expires = claims.get("exp")
    try:
        expiry = datetime.fromtimestamp(expires, timezone.utc).isoformat() if isinstance(expires, (float, int)) and not isinstance(expires, bool) else None
    except (ValueError, OverflowError, OSError):
        raise ImportError("Invalid OAuth token expiry.") from None
    last = value.get("last_refresh")
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError()
            last = dt.astimezone(timezone.utc).isoformat()
        except ValueError:
            raise ImportError("Invalid OAuth refresh timestamp.") from None
    else:
        # Do not invent a recent refresh time for an old export.
        last = "1970-01-01T00:00:00+00:00"
    auth = {"auth_mode": "chatgpt", "OPENAI_API_KEY": None,
            "tokens": {"id_token": identity_token, "access_token": access,
                       "refresh_token": refresh, "account_id": account_id}, "last_refresh": last}
    masked = f"{email[:1]}***@{email.partition('@')[2][:1]}***" if "@" in email else "(unknown)"
    preview = {"identity": masked, "expiresAt": expiry, "refreshCapable": bool(refresh),
               "expired": expires < datetime.now(timezone.utc).timestamp() if expiry else None,
               "verification": "parsed-unverified-claims"}
    user_id = _clean(auth_claim.get("chatgpt_user_id") or auth_claim.get("user_id") or identity.get("sub"))
    fingerprint = hashlib.sha256((account_id + "\0" + user_id).encode()).hexdigest()
    return auth, preview, fingerprint


class WindowsKey:
    """Access exactly one official profile passphrase; never enumerate keyrings."""

    def __init__(self, home: Path):
        if os.name != "nt":
            raise ImportError("OAuth file import currently supports Windows; use official login on this platform.")
        import nt
        import ctypes as c
        from ctypes import wintypes as w
        canonical = nt._getfinalpathname(str(home))
        self.account = "secrets|" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
        self.target = self.account + ".codex"
        class Credential(c.Structure):
            _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR),
                        ("Comment", w.LPWSTR), ("LastWritten", w.FILETIME),
                        ("CredentialBlobSize", w.DWORD), ("CredentialBlob", c.POINTER(c.c_ubyte)),
                        ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                        ("Attributes", c.c_void_p), ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR)]
        self.c, self.Credential = c, Credential
        self.dll = c.WinDLL("advapi32", use_last_error=True)
        self.dll.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(c.POINTER(Credential))]
        self.dll.CredReadW.restype = w.BOOL
        self.dll.CredWriteW.argtypes = [c.POINTER(Credential), w.DWORD]
        self.dll.CredWriteW.restype = w.BOOL
        self.dll.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
        self.dll.CredDeleteW.restype = w.BOOL
        self.dll.CredFree.argtypes = [c.c_void_p]

    def read(self) -> str | None:
        c = self.c
        pointer = c.POINTER(self.Credential)()
        if not self.dll.CredReadW(self.target, 1, 0, c.byref(pointer)):
            if c.get_last_error() == 1168:
                return None
            raise ImportError("Could not read this profile's Windows credential.")
        try:
            blob = pointer.contents
            return c.string_at(blob.CredentialBlob, blob.CredentialBlobSize).decode("utf-16-le")
        except UnicodeError:
            raise ImportError("Profile keyring entry has an unsupported encoding.") from None
        finally:
            self.dll.CredFree(pointer)

    def write(self, password: str) -> None:
        c = self.c
        raw = password.encode("utf-16-le")
        buf = (c.c_ubyte * len(raw)).from_buffer_copy(raw)
        entry = self.Credential(Type=1, TargetName=self.target, UserName=self.account,
                                CredentialBlobSize=len(raw), CredentialBlob=buf, Persist=2)
        if not self.dll.CredWriteW(c.byref(entry), 0):
            raise ImportError("Could not save this profile's Windows credential.")

    def delete(self) -> None:
        if not self.dll.CredDeleteW(self.target, 1, 0) and self.c.get_last_error() != 1168:
            raise ImportError("Could not remove the temporary profile credential.")


def _atomic(path: Path, data: bytes) -> None:
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with temp.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def save_auth(home: Path, auth: dict[str, Any]) -> Iterator[None]:
    """Commit on successful caller verification/registry save; roll back on error."""
    _crypto()
    directory = home / "secrets"
    path = directory / "codex_auth.age"
    if home.resolve() != home.absolute() or path.resolve() != path.absolute():
        raise ImportError("Refusing redirected authentication storage.")
    if (home / "auth.json").exists():
        raise ImportError("Existing legacy auth.json must be handled through official login before import.")
    directory.mkdir(parents=True, exist_ok=True)
    key = WindowsKey(home)
    password = key.read()
    original = None
    if path.exists():
        with path.open("rb") as stream:
            original = stream.read(MAX_BYTES + 4097)
        if password is None:
            raise ImportError("Encrypted auth exists without its key; use official login recovery.")
    document: dict[str, Any] = {"version": 1, "secrets": {}}
    if original is not None:
        try:
            document = json.loads(decrypt(original, password))
            if not isinstance(document, dict) or document.get("version") not in (0, 1) or not isinstance(document.get("secrets"), dict):
                raise ValueError()
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in document["secrets"].items()):
                raise ValueError()
        except (ValueError, TypeError):
            raise ImportError("Unsupported or unreadable existing auth store; nothing was replaced.") from None
    new_key = password is None
    if new_key and any(directory.glob("*.age")):
        raise ImportError("Other encrypted secrets exist without a key; refusing to replace their key.")
    password = password or base64.b64encode(os.urandom(32)).decode("ascii")
    document["version"] = 1
    document["secrets"]["global/CODEX_AUTH"] = json.dumps(auth, separators=(",", ":"))
    plaintext = json.dumps(document, separators=(",", ":")).encode()
    encrypted = encrypt(plaintext, password)
    wrote = False
    try:
        if new_key:
            key.write(password)
            if key.read() != password:
                raise ImportError("Profile credential readback failed.")
        if original is not None:
            backup = directory / ("codex_auth.before-import-" + uuid.uuid4().hex + ".age")
            with backup.open("xb") as stream:
                stream.write(original)
            if backup.read_bytes() != original:
                raise ImportError("Encrypted authentication backup verification failed.")
        _atomic(path, encrypted)
        wrote = True
        if decrypt(path.read_bytes(), password) != plaintext:
            raise ImportError("Encrypted authentication readback failed.")
        yield
    except BaseException:
        if wrote:
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, original)
        if new_key:
            key.delete()
        raise
