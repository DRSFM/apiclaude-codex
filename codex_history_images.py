"""Recover Codex clipboard images from durable session JSONL history.

Codex stores the image bytes in ``response_item`` records, while the Desktop
history view may refer to a short-lived ``codex-clipboard-*`` file.  This
module rebuilds only those validated files.  It never edits session history
or any Codex configuration/authentication state.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import json
import os
import re
import stat as stat_module
import tempfile
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


INDEX_SCHEMA_VERSION = 1
DEFAULT_MAX_IMAGE_BYTES = 50 * 1024 * 1024
MAX_LINE_OVERHEAD_BYTES = 1024 * 1024
ANCHOR_BYTES = 4096

_IMAGE_MARKER_RE = re.compile(
    r'^<image\b'
    r'(?=[^>\r\n]*\bname=\[Image #[1-9][0-9]*\])'
    r'(?=[^>\r\n]*\bpath="(?P<path>[^"]+)")'
    r'[^>\r\n]*>$',
    re.IGNORECASE,
)
_CLIPBOARD_NAME_RE = re.compile(
    r'^codex-clipboard-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
    r'[0-9a-f]{4}-[0-9a-f]{12}\.(?P<extension>png|jpe?g|gif|webp|avif)$',
    re.IGNORECASE,
)
_DATA_URL_RE = re.compile(
    r'^data:(?P<mime>image/[a-z0-9.+-]+);base64,(?P<data>[A-Za-z0-9+/=\s]+)$',
    re.IGNORECASE,
)
_MIME_EXTENSIONS = {
    "image/png": {"png"},
    "image/jpeg": {"jpg", "jpeg"},
    "image/gif": {"gif"},
    "image/webp": {"webp"},
    "image/avif": {"avif"},
}


@dataclass(frozen=True)
class ImageEntry:
    """A validated pointer to one embedded image in a session line."""

    target: str
    source: str
    line_offset: int
    content_index: int
    mime_type: str
    sha256: str
    byte_length: int

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "lineOffset": self.line_offset,
            "contentIndex": self.content_index,
            "mimeType": self.mime_type,
            "sha256": self.sha256,
            "byteLength": self.byte_length,
        }

    @classmethod
    def from_json(cls, source: str, value: Any) -> "ImageEntry" | None:
        if not isinstance(value, dict):
            return None
        target = value.get("target")
        line_offset = value.get("lineOffset")
        content_index = value.get("contentIndex")
        mime_type = value.get("mimeType")
        sha256 = value.get("sha256")
        byte_length = value.get("byteLength")
        if not (
            isinstance(target, str)
            and isinstance(line_offset, int)
            and line_offset >= 0
            and isinstance(content_index, int)
            and content_index >= 0
            and isinstance(mime_type, str)
            and isinstance(sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", sha256, re.IGNORECASE)
            and isinstance(byte_length, int)
            and byte_length >= 0
        ):
            return None
        return cls(
            target=target,
            source=source,
            line_offset=line_offset,
            content_index=content_index,
            mime_type=mime_type.lower(),
            sha256=sha256.lower(),
            byte_length=byte_length,
        )


@dataclass
class RepairReport:
    codex_home: str
    dry_run: bool = False
    session_files: int = 0
    scanned_files: int = 0
    incremental_files: int = 0
    rebuilt_files: int = 0
    unchanged_files: int = 0
    indexed_images: int = 0
    restored: int = 0
    recoverable: int = 0
    already_present: int = 0
    skipped: int = 0
    conflicts: int = 0
    rejected: int = 0
    stale: int = 0
    errors: int = 0
    issues: list[str] = field(default_factory=list)

    def issue(self, message: str, *, rejected: bool = False, error: bool = False) -> None:
        if rejected:
            self.rejected += 1
        if error:
            self.errors += 1
        if len(self.issues) < 80:
            self.issues.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "codexHome": self.codex_home,
            "dryRun": self.dry_run,
            "sessionFiles": self.session_files,
            "scannedFiles": self.scanned_files,
            "incrementalFiles": self.incremental_files,
            "rebuiltFiles": self.rebuilt_files,
            "unchangedFiles": self.unchanged_files,
            "indexedImages": self.indexed_images,
            "restored": self.restored,
            "recoverable": self.recoverable,
            "alreadyPresent": self.already_present,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
            "rejected": self.rejected,
            "stale": self.stale,
            "errors": self.errors,
            "issues": list(self.issues),
        }


def default_state_root(local_app_data: str | Path | None = None) -> Path:
    """Return a tool-owned state root, never a Codex home directory."""

    if local_app_data:
        return Path(local_app_data).expanduser() / "apicodex" / "history-images"
    configured = os.environ.get("LOCALAPPDATA", "").strip()
    if configured:
        return Path(configured) / "apicodex" / "history-images"
    return Path.home() / ".apicodex-image-repair"


def index_path_for_home(codex_home: Path, state_root: Path | None = None) -> Path:
    home = codex_home.expanduser().resolve()
    root = (state_root or default_state_root()).expanduser().resolve()
    digest = hashlib.sha256(os.path.normcase(str(home)).encode("utf-8")).hexdigest()[:20]
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", home.name) or "codex-home"
    return root / f"{label}-{digest}" / "index.json"


def repair_codex_history_images(
    codex_home: Path,
    *,
    state_root: Path | None = None,
    temp_root: Path | None = None,
    protected_roots: Iterable[Path] = (),
    dry_run: bool = False,
    max_image_bytes: int = DEFAULT_MAX_IMAGE_BYTES,
) -> RepairReport:
    """Index and restore valid clipboard images for one Codex home.

    Only ``<CODEX_HOME>/sessions`` is read.  The optional state index is kept
    outside that home, and writes are limited to validated files directly
    under ``temp_root``.
    """

    home = Path(codex_home).expanduser().resolve()
    sessions_root = home / "sessions"
    temp = Path(temp_root) if temp_root is not None else Path(tempfile.gettempdir())
    temp = temp.expanduser().resolve()
    protected = [Path(root).expanduser().resolve() for root in protected_roots]
    report = RepairReport(codex_home=str(home), dry_run=dry_run)
    if max_image_bytes <= 0:
        report.issue("max_image_bytes must be positive", error=True)
        return report
    if not sessions_root.is_dir():
        return report
    try:
        if sessions_root.is_symlink() or _canonical(str(sessions_root.resolve())) != _canonical(str(sessions_root)):
            report.issue("refusing to traverse a linked sessions directory", error=True)
            return report
        if _is_inside(temp, home) or any(_is_inside(temp, root) for root in protected):
            report.issue("refusing to use a Temp root inside CODEX_HOME", error=True)
            return report
        temp_identity = _directory_identity(temp)
        if temp_identity is None:
            report.issue("refusing to use an unavailable Temp root", error=True)
            return report
    except (OSError, RuntimeError, ValueError) as exc:
        report.issue(f"cannot validate history roots: {exc}", error=True)
        return report

    try:
        files = sorted(
            path for path in sessions_root.rglob("*.jsonl")
            if path.is_file() and not path.is_symlink()
            and _is_inside(path, sessions_root)
        )
    except OSError as exc:
        report.issue(f"cannot enumerate sessions: {exc}", error=True)
        return report
    report.session_files = len(files)

    index_path = index_path_for_home(home, state_root)
    persist_index = not (
        _is_inside(index_path, home)
        or any(_is_inside(index_path, root) for root in protected)
    )
    if not persist_index:
        report.issue("index root is inside CODEX_HOME; continuing without saving it", error=True)
    index, index_valid = (
        _load_index(index_path, home, sessions_root, temp)
        if persist_index
        else (_new_index(home, sessions_root, temp), False)
    )
    if not index_valid:
        report.issue("history image index was missing or invalid; rebuilding")

    indexed_files = index["files"]
    current_keys: set[str] = set()
    for source_path in files:
        relative = source_path.relative_to(sessions_root).as_posix()
        current_keys.add(relative)
        try:
            stat = source_path.stat()
        except OSError as exc:
            report.issue(f"cannot stat session {relative}: {exc}", error=True)
            continue
        old = indexed_files.get(relative)
        if _is_unchanged_file(source_path, old, stat):
            report.unchanged_files += 1
            continue

        start_offset = 0
        old_images: list[dict[str, Any]] = []
        if _can_append_file(source_path, old, stat):
            start_offset = int(old.get("indexedOffset", 0))
            old_images = list(old.get("images") or [])
            report.incremental_files += 1
        else:
            if old is not None:
                report.rebuilt_files += 1

        report.scanned_files += 1
        scan_result = _scan_session_file(
            source_path,
            relative,
            temp,
            start_offset=start_offset,
            max_image_bytes=max_image_bytes,
            report=report,
        )
        if scan_result is None:
            # A transient sharing violation or concurrent rewrite must not
            # replace a previously usable index with an empty partial result.
            continue
        new_images, indexed_offset, content_hash = scan_result
        try:
            final_stat = source_path.stat()
        except OSError as exc:
            report.issue(f"cannot restat session {relative}: {exc}", error=True)
            continue
        if not _same_file_snapshot(stat, final_stat):
            report.issue(
                f"session changed while scanning; preserving previous index: {relative}",
                error=True,
            )
            continue
        all_images = old_images + [entry.to_json() for entry in new_images]
        indexed_files[relative] = {
            "size": final_stat.st_size,
            "mtimeNs": final_stat.st_mtime_ns,
            "indexedOffset": indexed_offset,
            "anchor": _anchor_digest(source_path, indexed_offset),
            "contentHash": content_hash,
            "images": all_images,
        }

    for stale_key in set(indexed_files) - current_keys:
        del indexed_files[stale_key]

    entries = _collect_entries(indexed_files, report)
    report.indexed_images = len(entries)
    if not dry_run and persist_index:
        try:
            _save_index(index_path, index)
        except OSError as exc:
            # The cache is an optimization; a state-directory failure must not
            # prevent a safe one-shot restoration.
            report.issue(f"cannot save history image index: {exc}", error=True)

    _restore_entries(
        entries,
        home=home,
        sessions_root=sessions_root,
        temp_root=temp,
        temp_identity=temp_identity,
        max_image_bytes=max_image_bytes,
        dry_run=dry_run,
        report=report,
    )
    return report


def _new_index(home: Path, sessions_root: Path, temp_root: Path) -> dict[str, Any]:
    return {
        "schemaVersion": INDEX_SCHEMA_VERSION,
        "codexHome": str(home),
        "sessionsRoot": str(sessions_root),
        "tempRoot": str(temp_root),
        "files": {},
    }


def _load_index(
    path: Path,
    home: Path,
    sessions_root: Path,
    temp_root: Path,
) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return _new_index(home, sessions_root, temp_root), False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("schemaVersion") != INDEX_SCHEMA_VERSION
            or _canonical(value.get("codexHome", "")) != _canonical(str(home))
            or _canonical(value.get("sessionsRoot", "")) != _canonical(str(sessions_root))
            or _canonical(value.get("tempRoot", "")) != _canonical(str(temp_root))
            or not isinstance(value.get("files"), dict)
        ):
            raise ValueError("metadata mismatch")
        # Discard malformed file entries instead of trusting them as paths.
        files: dict[str, Any] = {}
        malformed = False
        for relative, info in value["files"].items():
            if not isinstance(relative, str) or not _safe_relative_source(relative):
                malformed = True
                continue
            if not isinstance(info, dict):
                malformed = True
                continue
            if not all(isinstance(info.get(key), int) and info.get(key) >= 0 for key in ("size", "mtimeNs", "indexedOffset")):
                malformed = True
                continue
            if not isinstance(info.get("anchor", ""), str):
                malformed = True
                continue
            content_hash = info.get("contentHash")
            if content_hash is not None and not (
                isinstance(content_hash, str)
                and re.fullmatch(r"[0-9a-f]{64}", content_hash, re.IGNORECASE)
            ):
                malformed = True
                continue
            images = info.get("images", [])
            if not isinstance(images, list):
                malformed = True
                continue
            if any(ImageEntry.from_json(relative, image) is None for image in images):
                malformed = True
                continue
            files[relative] = {**info, "images": images}
        if malformed:
            raise ValueError("malformed file entry")
        value["files"] = files
        return value, True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return _new_index(home, sessions_root, temp_root), False


def _save_index(path: Path, index: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(index, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _is_unchanged_file(path: Path, info: Any, stat: os.stat_result) -> bool:
    if not (
        isinstance(info, dict)
        and info.get("size") == stat.st_size
        and info.get("mtimeNs") == stat.st_mtime_ns
        and info.get("indexedOffset") == stat.st_size
        and isinstance(info.get("contentHash"), str)
    ):
        return False
    digest = _content_digest(path, stat.st_size)
    if digest is None or digest != info["contentHash"]:
        return False
    try:
        return _same_file_snapshot(stat, path.stat())
    except OSError:
        return False


def _can_append_file(path: Path, info: Any, stat: os.stat_result) -> bool:
    if not isinstance(info, dict):
        return False
    old_size = info.get("size")
    offset = info.get("indexedOffset")
    if not isinstance(old_size, int) or not isinstance(offset, int):
        return False
    if old_size > stat.st_size or offset < 0 or offset > old_size:
        return False
    if old_size == stat.st_size:
        return False
    expected = info.get("anchor")
    expected_content = info.get("contentHash")
    if not (
        isinstance(expected, str)
        and expected == _anchor_digest(path, offset)
        and isinstance(expected_content, str)
        and re.fullmatch(r"[0-9a-f]{64}", expected_content, re.IGNORECASE)
    ):
        return False
    return expected_content == _content_digest(path, old_size)


def _scan_session_file(
    path: Path,
    relative: str,
    temp_root: Path,
    *,
    start_offset: int,
    max_image_bytes: int,
    report: RepairReport,
) -> tuple[list[ImageEntry], int, str] | None:
    entries: list[ImageEntry] = []
    try:
        scan_size = path.stat().st_size
        with path.open("rb") as handle:
            digest = hashlib.sha256()
            prefix_remaining = start_offset
            while prefix_remaining:
                chunk = handle.read(min(prefix_remaining, 1024 * 1024))
                if not chunk:
                    raise OSError("session ended before the indexed offset")
                digest.update(chunk)
                prefix_remaining -= len(chunk)
            indexed_offset = start_offset
            max_line = max_image_bytes * 4 // 3 + MAX_LINE_OVERHEAD_BYTES
            while handle.tell() < scan_size:
                line_offset = handle.tell()
                remaining = scan_size - line_offset
                raw = handle.readline(min(remaining, max_line + 1))
                if not raw:
                    break
                digest.update(raw)
                if len(raw) > max_line and not raw.endswith(b"\n"):
                    # Consume the rest of an oversized line, but keep the
                    # scanner isolated from subsequent valid JSONL records.
                    while handle.tell() < scan_size:
                        chunk = handle.readline(min(scan_size - handle.tell(), 64 * 1024))
                        digest.update(chunk)
                        if not chunk or chunk.endswith(b"\n"):
                            break
                    indexed_offset = handle.tell()
                    report.issue(f"oversized session line skipped: {relative}:{line_offset}", rejected=True)
                    continue
                if not raw.endswith(b"\n"):
                    # A live session may end in a partially written record.
                    break
                indexed_offset = handle.tell()
                if b'"input_image"' not in raw or b'"response_item"' not in raw:
                    continue
                try:
                    row = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    report.issue(f"invalid JSONL image candidate skipped: {relative}:{line_offset}", rejected=True)
                    continue
                entries.extend(
                    _extract_row_entries(
                        row,
                        relative,
                        line_offset,
                        temp_root,
                        max_image_bytes=max_image_bytes,
                        report=report,
                    )
                )
            if handle.tell() != scan_size:
                raise OSError("session changed while it was being scanned")
            return entries, indexed_offset, digest.hexdigest()
    except OSError as exc:
        report.issue(f"cannot scan session {relative}: {exc}", error=True)
        return None


def _extract_row_entries(
    row: Any,
    source: str,
    line_offset: int,
    temp_root: Path,
    *,
    max_image_bytes: int,
    report: RepairReport,
) -> list[ImageEntry]:
    if not isinstance(row, dict) or row.get("type") != "response_item":
        return []
    payload = row.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []

    entries: list[ImageEntry] = []
    for index, item in enumerate(content):
        if not isinstance(item, dict) or item.get("type") != "input_image" or index == 0:
            continue
        marker = content[index - 1]
        if not isinstance(marker, dict) or marker.get("type") != "input_text":
            continue
        marker_text = marker.get("text")
        if not isinstance(marker_text, str):
            continue
        match = _IMAGE_MARKER_RE.fullmatch(marker_text.strip())
        if not match:
            continue
        raw_target = html.unescape(match.group("path"))
        target, path_error = _validated_target(raw_target, temp_root)
        if target is None:
            if path_error in {"outside-temp", "invalid-name"}:
                report.skipped += 1
            else:
                report.issue(f"rejected image target in {source}:{line_offset}", rejected=True)
            continue
        try:
            data, mime_type = _decode_image_item(item, max_image_bytes)
            expected_extensions = _MIME_EXTENSIONS[mime_type]
            if target.suffix.lower().lstrip(".") not in expected_extensions:
                raise ValueError("file extension does not match MIME type")
            if not _has_image_signature(data, mime_type):
                raise ValueError("image signature does not match MIME type")
        except (ValueError, KeyError, binascii.Error):
            report.issue(f"rejected image data in {source}:{line_offset}", rejected=True)
            continue
        entries.append(
            ImageEntry(
                target=str(target),
                source=source,
                line_offset=line_offset,
                content_index=index,
                mime_type=mime_type,
                sha256=hashlib.sha256(data).hexdigest(),
                byte_length=len(data),
            )
        )
    return entries


def _decode_image_item(item: dict[str, Any], max_image_bytes: int) -> tuple[bytes, str]:
    value = item.get("image_url") or item.get("url")
    mime_hint: str | None = None
    source = item.get("source")
    if isinstance(source, dict):
        mime_hint = source.get("media_type") or source.get("mime_type")
        value = source.get("url") or source.get("data") or value
        if isinstance(value, str) and not value.startswith("data:") and source.get("data"):
            value = f"data:{mime_hint or 'image/png'};base64,{value}"
    if not isinstance(value, str):
        raise ValueError("missing image data")
    match = _DATA_URL_RE.fullmatch(value.strip())
    if not match:
        raise ValueError("image is not a base64 data URL")
    mime_type = match.group("mime").lower()
    if mime_type not in _MIME_EXTENSIONS:
        raise ValueError("unsupported image MIME type")
    encoded = re.sub(r"\s+", "", match.group("data"))
    if len(encoded) > (max_image_bytes * 4 // 3) + 4:
        raise ValueError("image exceeds size limit")
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > max_image_bytes:
        raise ValueError("image exceeds size limit")
    return data, mime_type


def _has_image_signature(data: bytes, mime_type: str) -> bool:
    if mime_type == "image/png":
        return _has_complete_png(data)
    if mime_type == "image/jpeg":
        return (
            len(data) >= 4
            and data.startswith(b"\xff\xd8\xff")
            and data.endswith(b"\xff\xd9")
        )
    if mime_type == "image/gif":
        return (
            len(data) >= 14
            and data.startswith((b"GIF87a", b"GIF89a"))
            and data.endswith(b";")
        )
    if mime_type == "image/webp":
        return (
            len(data) >= 16
            and data[:4] == b"RIFF"
            and data[8:12] == b"WEBP"
            and int.from_bytes(data[4:8], "little") == len(data) - 8
        )
    if mime_type == "image/avif":
        return _has_complete_avif(data)
    return False


def _has_complete_png(data: bytes) -> bool:
    """Check PNG chunk bounds and CRCs, not only its eight-byte signature."""

    signature = b"\x89PNG\r\n\x1a\n"
    if len(data) < len(signature) + 12 or not data.startswith(signature):
        return False
    offset = len(signature)
    saw_header = False
    saw_data = False
    while offset + 12 <= len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        end = offset + 12 + length
        if end > len(data):
            return False
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        crc = int.from_bytes(data[offset + 8 + length : end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != crc:
            return False
        if not saw_header:
            if kind != b"IHDR" or length != 13:
                return False
            width = int.from_bytes(payload[:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            if width == 0 or height == 0:
                return False
            saw_header = True
        elif kind == b"IDAT":
            saw_data = True
        if kind == b"IEND":
            return length == 0 and saw_data and end == len(data)
        offset = end
    return False


def _has_complete_avif(data: bytes) -> bool:
    """Validate the ISO-BMFF box envelope and AVIF brand declaration."""

    if len(data) < 16 or data[4:8] != b"ftyp":
        return False
    offset = 0
    saw_ftyp = False
    while offset + 8 <= len(data):
        size = int.from_bytes(data[offset : offset + 4], "big")
        kind = data[offset + 4 : offset + 8]
        header_size = 8
        if size == 1:
            if offset + 16 > len(data):
                return False
            size = int.from_bytes(data[offset + 8 : offset + 16], "big")
            header_size = 16
        elif size == 0:
            size = len(data) - offset
        if size < header_size or offset + size > len(data):
            return False
        if kind == b"ftyp":
            payload = data[offset + header_size : offset + size]
            if len(payload) < 8:
                return False
            brands = [payload[:4]]
            brands.extend(
                payload[index : index + 4]
                for index in range(8, len(payload) - 3, 4)
            )
            if not any(brand in {b"avif", b"avis"} for brand in brands):
                return False
            saw_ftyp = True
        offset += size
    return saw_ftyp and offset == len(data)


def _validated_target(raw: str, temp_root: Path) -> tuple[Path | None, str | None]:
    if not raw or "\x00" in raw:
        return None, "empty-or-nul"
    candidate = Path(raw).expanduser()
    if ".." in candidate.parts:
        return None, "parent-traversal"
    try:
        target = Path(os.path.abspath(os.fspath(candidate)))
        temp_resolved = temp_root.resolve()
        if target.parent.resolve() != temp_resolved:
            return None, "outside-temp"
    except (OSError, RuntimeError, ValueError):
        return None, "invalid-path"
    match = _CLIPBOARD_NAME_RE.fullmatch(target.name)
    if not match:
        return None, "invalid-name"
    return target, None


def _collect_entries(indexed_files: dict[str, Any], report: RepairReport) -> dict[str, list[ImageEntry]]:
    grouped: dict[str, list[ImageEntry]] = {}
    for source, info in indexed_files.items():
        if not _safe_relative_source(source) or not isinstance(info, dict):
            continue
        for value in info.get("images", []):
            entry = ImageEntry.from_json(source, value)
            if entry is None:
                report.issue(f"invalid image index entry skipped: {source}", rejected=True)
                continue
            grouped.setdefault(_canonical(entry.target), []).append(entry)
    return grouped


def _restore_entries(
    grouped: dict[str, list[ImageEntry]],
    *,
    home: Path,
    sessions_root: Path,
    temp_root: Path,
    temp_identity: tuple[int, int] | None,
    max_image_bytes: int,
    dry_run: bool,
    report: RepairReport,
) -> None:
    for target_key, candidates in grouped.items():
        if temp_identity is None or _directory_identity(temp_root) != temp_identity:
            report.issue("Temp root changed while restoring history images", error=True)
            return
        validated: list[tuple[ImageEntry, Path]] = []
        for candidate in candidates:
            candidate_target, _ = _validated_target(candidate.target, temp_root)
            if (
                candidate_target is not None
                and _canonical(str(candidate_target)) == target_key
            ):
                validated.append((candidate, candidate_target))
        if not validated:
            report.issue(f"rejected indexed target: {target_key}", rejected=True)
            continue

        hashes = {entry.sha256 for entry, _ in validated}
        if len(hashes) != 1:
            report.conflicts += 1
            report.issue(f"conflicting image sources for {target_key}")
            continue
        entry, target = validated[0]
        if target.is_symlink() or (target.exists() and not target.is_file()):
            report.conflicts += 1
            report.issue(f"existing non-regular target not overwritten: {target.name}")
            continue
        if target.is_file():
            try:
                expected_lengths = {item.byte_length for item, _ in validated}
                if (
                    _sha256_file(target) == entry.sha256
                    and target.stat().st_size in expected_lengths
                ):
                    report.already_present += 1
                else:
                    report.conflicts += 1
                    report.issue(f"existing image differs; not overwritten: {target.name}")
            except OSError as exc:
                report.errors += 1
                report.issue(f"cannot inspect existing target {target.name}: {exc}")
            continue

        data: bytes | None = None
        for candidate, candidate_target in validated:
            candidate_data = _read_indexed_image(
                candidate,
                sessions_root=sessions_root,
                temp_root=temp_root,
                max_image_bytes=max_image_bytes,
                report=report,
            )
            if candidate_data is not None:
                entry = candidate
                target = candidate_target
                data = candidate_data
                break
        if data is None:
            report.stale += 1
            continue
        if dry_run:
            report.recoverable += 1
            continue
        try:
            result = _write_new_file(
                target,
                data,
                temp_root=temp_root,
                temp_identity=temp_identity,
            )
        except OSError as exc:
            report.errors += 1
            report.issue(f"cannot restore {target.name}: {exc}")
            continue
        if result == "restored":
            report.restored += 1
        elif result == "exists":
            report.conflicts += 1
            report.issue(f"target appeared during restore; not overwritten: {target.name}")


def _read_indexed_image(
    entry: ImageEntry,
    *,
    sessions_root: Path,
    temp_root: Path,
    max_image_bytes: int,
    report: RepairReport,
) -> bytes | None:
    if not _safe_relative_source(entry.source):
        report.issue(f"rejected indexed source: {entry.source}", rejected=True)
        return None
    source_path = sessions_root / Path(entry.source)
    if not source_path.is_file() or source_path.is_symlink() or not _is_inside(source_path, sessions_root):
        report.issue(f"indexed source is unavailable: {entry.source}")
        return None
    try:
        with source_path.open("rb") as handle:
            handle.seek(entry.line_offset)
            raw = handle.readline()
        row = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        report.issue(f"indexed source line is invalid: {entry.source}:{entry.line_offset}")
        return None
    candidates = _extract_row_entries(
        row,
        entry.source,
        entry.line_offset,
        temp_root,
        max_image_bytes=max_image_bytes,
        report=report,
    )
    matching = [item for item in candidates if item.content_index == entry.content_index]
    if len(matching) != 1:
        report.issue(f"indexed image no longer matches: {entry.source}:{entry.line_offset}")
        return None
    current = matching[0]
    if current.target != entry.target or current.mime_type != entry.mime_type or current.sha256 != entry.sha256:
        report.issue(f"indexed image changed: {entry.source}:{entry.line_offset}")
        return None
    # Decode once more from the validated row so the bytes written are the
    # bytes whose hash was checked, rather than trusting the index metadata.
    try:
        payload = json.loads(raw.decode("utf-8"))
        content = payload["payload"]["content"]
        data, mime_type = _decode_image_item(content[entry.content_index], max_image_bytes)
        if mime_type != entry.mime_type or hashlib.sha256(data).hexdigest() != entry.sha256:
            report.issue(f"indexed image verification failed: {entry.source}:{entry.line_offset}")
            return None
        return data
    except (KeyError, IndexError, TypeError, ValueError, binascii.Error):
        report.issue(f"indexed image payload is invalid: {entry.source}:{entry.line_offset}")
        return None


def _write_new_file(
    target: Path,
    data: bytes,
    *,
    temp_root: Path | None = None,
    temp_identity: tuple[int, int] | None = None,
) -> str:
    if temp_root is not None:
        if temp_identity is None or _directory_identity(temp_root) != temp_identity:
            raise OSError("Temp root changed while restoring image")
        try:
            if target.parent.resolve() != temp_root.resolve():
                raise OSError("image target moved outside Temp root")
        except (OSError, RuntimeError, ValueError) as exc:
            raise OSError("image target moved outside Temp root") from exc
    staging = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        with staging.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if temp_root is not None and _directory_identity(temp_root) != temp_identity:
            raise OSError("Temp root changed while restoring image")
        if os.name == "nt":
            # Windows rename refuses to replace an existing destination.
            os.rename(staging, target)
        else:
            os.link(staging, target)
            staging.unlink()
        return "restored"
    except FileExistsError:
        return "exists"
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_digest(path: Path, byte_length: int) -> str | None:
    """Hash an exact file prefix, returning None if it cannot be read fully."""

    digest = hashlib.sha256()
    remaining = byte_length
    try:
        with path.open("rb") as handle:
            while remaining:
                chunk = handle.read(min(remaining, 1024 * 1024))
                if not chunk:
                    return None
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _directory_identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
        if not stat_module.S_ISDIR(info.st_mode):
            return None
        return (
            int(getattr(info, "st_dev", 0)),
            int(getattr(info, "st_ino", 0)),
        )
    except OSError:
        return None


def _same_file_snapshot(before: os.stat_result, after: os.stat_result) -> bool:
    return all(
        getattr(before, name, None) == getattr(after, name, None)
        for name in ("st_size", "st_mtime_ns", "st_ctime_ns", "st_dev", "st_ino")
    )


def _anchor_digest(path: Path, offset: int) -> str:
    if offset <= 0:
        return hashlib.sha256(b"").hexdigest()
    try:
        start = max(0, offset - ANCHOR_BYTES)
        with path.open("rb") as handle:
            handle.seek(start)
            data = handle.read(offset - start)
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return ""


def _safe_relative_source(value: str) -> bool:
    if not value or "\x00" in value:
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts and path.suffix.lower() == ".jsonl"


def _is_inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False


def _canonical(value: str) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(value)))
