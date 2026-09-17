"""Default-account resources shared by named accounts, never authentication.

Directory junctions work on Windows without developer-mode symlink privileges.
Config and instruction files follow the default source on each wrapper launch.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any
import uuid

from codex_shared_config import _split_toml_dotted_key


class ResourceError(ValueError):
    pass


DIRECTORIES = ("skills", "rules", "prompts", "plugins/cache")
FILES = ("AGENTS.md", "AGENTS.override.md", "hooks.json")
SHARED_CONFIG = frozenset({
    "mcp_servers", "plugins", "marketplaces", "skills", "features", "tui",
    "approval_policy", "approvals_reviewer", "sandbox_mode", "sandbox_workspace_write",
    "shell_environment_policy", "windows", "desktop", "notify", "web_search",
    "project_doc_max_bytes", "project_doc_fallback_filenames",
})


def _neutral_after(line: str, state: tuple[str | None, int]) -> tuple[str | None, int]:
    """Track multiline TOML values so their contents cannot become table headers."""
    quote, depth = state
    i = 0
    while i < len(line):
        if quote:
            if quote.startswith('"') and line[i] == "\\":
                i += 2
                continue
            if line.startswith(quote, i):
                i += len(quote)
                quote = None
                continue
        else:
            if line[i] == "#":
                break
            if line[i] in "\"'":
                quote = line[i] * (3 if line.startswith(line[i] * 3, i) else 1)
                i += len(quote)
                continue
            if line[i] in "[{":
                depth += 1
            elif line[i] in "]}":
                depth -= 1
        i += 1
    return quote, depth


def config_parts(text: str) -> list[tuple[str | None, str]]:
    parts: list[tuple[str | None, str]] = []
    lines: list[str] = []
    root = None
    in_table = False
    state: tuple[str | None, int] = (None, 0)
    for line in text.splitlines(keepends=True):
        key = None
        if state == (None, 0):
            header = re.fullmatch(r"\s*(\[\[?)(.*?)\]\]?\s*(?:#.*)?", line.rstrip("\r\n"))
            if header:
                key = _split_toml_dotted_key(header[2])
                in_table = True
            elif not in_table:
                assignment = re.match(r"\s*([^=#]+?)\s*=", line)
                if assignment:
                    key = _split_toml_dotted_key(assignment[1])
        if key:
            if lines:
                parts.append((root, "".join(lines)))
            root, lines = key[0], []
        lines.append(line)
        state = _neutral_after(line, state)
    if lines:
        parts.append((root, "".join(lines)))
    if state != (None, 0):
        raise ResourceError("Cannot safely parse shared configuration; source was not changed.")
    return parts


def _toml_fields(text: str, separator: str = '\n') -> list[str]:
    """Split assignments without touching strings, nested values or comments.

    Keep value text instead of serializing through a new TOML dependency; the
    launcher also supports Python 3.10, which has no standard TOML decoder.
    """
    fields, current = [], []
    quote = None
    depth = 0
    i = 0
    while i < len(text):
        char = text[i]
        if quote:
            if quote.startswith('"') and char == '\\':
                current.append(text[i:i + 2])
                i += 2
                continue
            if text.startswith(quote, i):
                current.append(quote)
                i += len(quote)
                quote = None
                continue
        else:
            if char == '#':
                end = text.find('\n', i)
                i = len(text) if end < 0 else end
                continue
            if char in "\"'":
                quote = char * (3 if text.startswith(char * 3, i) else 1)
                current.append(quote)
                i += len(quote)
                continue
            if char in '[{':
                depth += 1
            elif char in ']}':
                depth -= 1
            if char == separator and depth == 0:
                fields.append(''.join(current).strip())
                current = []
                i += 1
                continue
        current.append(char)
        i += 1
    if quote or depth:
        raise ResourceError("Cannot safely parse shared feature values.")
    fields.append(''.join(current).strip())
    return [field for field in fields if field]


def _feature_entries(block: str) -> dict[tuple[str, ...], str]:
    """Preserve nested features; exclude only the authentication-storage tree."""
    key = r'''(?:[A-Za-z0-9_-]+|"(?:[^"\\]|\\.)*"|'[^']*')'''
    entry = re.compile(rf'\s*({key}(?:\s*\.\s*{key})*)\s*=\s*(.+)', re.S)
    fields = _toml_fields(block)
    prefix: tuple[str, ...] = ()
    if fields and fields[0].startswith('['):
        header = re.fullmatch(r'\[(.*?)\]', fields.pop(0))
        if not header:
            raise ResourceError("Unsupported shared feature table.")
        prefix = _split_toml_dotted_key(header[1])
        if not prefix or prefix[0] != 'features':
            raise ResourceError("Unsupported shared feature table.")
    result = {}
    if not fields and len(prefix) > 1 and prefix[1] != 'secret_auth_storage':
        result[prefix[1:]] = '{}'
    for field in fields:
        match = entry.fullmatch(field)
        if not match:
            raise ResourceError("Cannot safely parse shared feature assignment.")
        keys = _split_toml_dotted_key(match[1])
        if not keys:
            raise ResourceError("Cannot safely parse shared feature key.")
        parts = prefix + keys
        value = match[2].strip()
        if parts == ('features',) and value.startswith('{') and value.endswith('}'):
            # The root inline table may contain a protected auth flag alongside
            # arbitrary nested settings. Split only its immediate members.
            inline = '\n'.join(_toml_fields(value[1:-1], ','))
            result.update(_feature_entries('[features]\n' + inline))
        elif len(parts) > 1 and parts[0] == 'features':
            if parts[1] != 'secret_auth_storage':
                result[parts[1:]] = value
        else:
            raise ResourceError("Unsupported shared feature key.")
    return result


def merge_config(target: str, source: str) -> str:
    selected = []
    features: dict[tuple[str, ...], str] = {}
    for root, block in config_parts(source):
        if root in SHARED_CONFIG:
            # A generic feature section must not disable the encrypted auth
            # backend used by named accounts. Keep its other feature settings.
            if root == "features":
                features.update(_feature_entries(block))
                continue
            selected.append(block.strip())
    if features:
        # Empty parent tables can be implicit when child tables follow them.
        # Emitting parent = {} would seal it and make the child invalid TOML.
        features = {key: value for key, value in features.items()
                    if value != '{}' or not any(other[:len(key)] == key and other != key
                                                for other in features)}
        selected.append('[features]\n' + '\n'.join(
            f'{".".join(json.dumps(part, ensure_ascii=False) for part in key)} = {value}'
            for key, value in features.items()))
    independent = [block.strip() for root, block in config_parts(target) if root not in SHARED_CONFIG]
    # Root assignments must precede tables, including independently kept tables.
    all_parts = [p for p in [*independent, *selected] if p]
    assignments = [p for p in all_parts if not p.lstrip().startswith("[")]
    tables = [p for p in all_parts if p.lstrip().startswith("[")]
    merged = "\n\n".join([*assignments, *tables]).rstrip() + "\n"
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10: lexical checks above remain available.
        pass
    else:
        try:
            tomllib.loads(source)
            tomllib.loads(target)
            tomllib.loads(merged)
        except tomllib.TOMLDecodeError:
            raise ResourceError("Invalid shared/account TOML; configuration was not changed.") from None
    return merged


def _redirected(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def link_directory(target: Path, source: Path) -> None:
    if os.name == "nt":
        # Use a structured environment, never interpolate paths into shell code.
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            raise ResourceError("PowerShell is required to create shared directory junctions.")
        env = os.environ.copy()
        env.update(APICODEX_LINK_PATH=str(target), APICODEX_LINK_SOURCE=str(source))
        result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command",
            "$ErrorActionPreference='Stop'; New-Item -ItemType Junction -Path $env:APICODEX_LINK_PATH -Target $env:APICODEX_LINK_SOURCE | Out-Null"],
            env=env, capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ResourceError("Could not create the shared resource junction; original files were retained.")
    else:
        target.symlink_to(source, target_is_directory=True)
    if target.resolve() != source.resolve():
        raise ResourceError("Shared resource link verification failed.")


def _copy_missing(source: Path, target: Path, conflicts: list[str], *, relative: str = "") -> None:
    """Preserve local-only user resources; default files win conflicts, originals stay backed up."""
    if os.name == "nt":
        # Adding the backup prefix can push otherwise valid plugin paths past
        # MAX_PATH. Use extended absolute paths for traversal and file I/O;
        # is_file/is_dir can otherwise silently treat deep entries as missing.
        def extended(path: Path) -> Path:
            value = str(path.absolute())
            if value.startswith('\\\\?\\'):
                return path
            return Path('\\\\?\\UNC\\' + value[2:] if value.startswith('\\\\') else '\\\\?\\' + value)
        source, target = extended(source), extended(target)
    for child in source.iterdir():
        if not relative and child.name == ".system":
            continue  # generated official skills follow the default runtime
        name = f"{relative}/{child.name}".lstrip("/")
        destination = target / child.name
        if destination.exists():
            if child.is_dir() and destination.is_dir() and not _redirected(child):
                _copy_missing(child, destination, conflicts, relative=name)
            elif child.is_file() and destination.is_file() and not _redirected(child):
                if hashlib.sha256(child.read_bytes()).digest() != hashlib.sha256(destination.read_bytes()).digest():
                    conflicts.append(name)
            else:
                conflicts.append(name)
            continue
        if _redirected(child):
            # A retained backup is preferable to copying an unknown linked tree
            # into the default account's resource directory.
            conflicts.append(name)
            continue
        if child.is_dir():
            destination.mkdir()
            _copy_missing(child, destination, conflicts, relative=name)
        elif child.is_file():
            with destination.open("xb") as stream:
                stream.write(child.read_bytes())
            if child.read_bytes() != destination.read_bytes():
                raise ResourceError("Shared resource copy verification failed; original backup retained.")


def sync(home: Path, default_home: Path, api: Any, *, dry_run: bool = False) -> dict[str, Any]:
    if home.resolve() == default_home.resolve() or home.resolve() != home.absolute():
        raise ResourceError("Shared resources require a separate, stable account home.")
    config = home / "config.toml"
    if config.resolve() != config.absolute():
        raise ResourceError("The account's config.toml must remain independent.")
    # On a fresh machine the default account need not have a config yet.
    source_config = default_home / "config.toml"
    before = config.read_text(encoding="utf-8-sig")
    source_text = source_config.read_text(encoding="utf-8-sig") if source_config.exists() else None
    updated = merge_config(before, source_text) if source_text is not None else before
    report: dict[str, Any] = {"source": str(default_home), "home": str(home), "dryRun": dry_run,
                             "directories": list(DIRECTORIES), "configChanged": updated != before,
                             "conflictsRetainedInBackup": [], "deferredDirectories": [], "backup": None}
    for name in DIRECTORIES:
        target = home / name
        if target.resolve() != target.absolute() and target.resolve() != (default_home / name).resolve():
            raise ResourceError("A resource directory already points elsewhere; no link was replaced.")
    if dry_run:
        return report
    backup = home / ".account-resource-backups" / uuid.uuid4().hex
    def preserve(path: Path) -> Path:
        saved = backup / path.relative_to(home)
        saved.parent.mkdir(parents=True, exist_ok=True)
        path.rename(saved)
        report["backup"] = str(backup)
        return saved
    for name in DIRECTORIES:
        target, source = home / name, default_home / name
        source.mkdir(parents=True, exist_ok=True)
        if target.resolve() == source.resolve():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            saved = preserve(target) if target.exists() else None
        except PermissionError:
            if name != 'plugins/cache':
                raise
            # A running Desktop can keep package files open without delete
            # sharing. The next launch retries; keep all existing packages.
            report['deferredDirectories'].append(name)
            continue
        try:
            if saved:
                _copy_missing(saved, source, report["conflictsRetainedInBackup"])
            link_directory(target, source)
        except BaseException:
            if _redirected(target) and target.resolve() == source.resolve():
                if os.name == "nt":
                    target.rmdir()
                else:
                    target.unlink()
            if saved and not target.exists():
                saved.rename(target)
            raise
    for name in FILES:
        source, target = default_home / name, home / name
        if not source.is_file():
            # The default source is authoritative, including deletion. Retain
            # any former local/managed instruction in the migration backup.
            if target.exists():
                if _redirected(target):
                    raise ResourceError("An account shared resource file is redirected; no file was replaced.")
                preserve(target)
            continue
        if target.resolve() != target.absolute():
            if target.resolve() == source.resolve():
                continue
            raise ResourceError("An account shared resource file points elsewhere; no file was replaced.")
        content = source.read_text(encoding="utf-8-sig")
        if target.is_file() and target.read_text(encoding="utf-8-sig") == content:
            continue
        saved = preserve(target) if target.exists() else None
        try:
            api.write_text_atomic(target, content)
        except BaseException:
            if saved and not target.exists():
                saved.rename(target)
            raise
    if updated != before:
        backup.mkdir(parents=True, exist_ok=True)
        api.write_text_atomic(backup / "config.toml", before)
        report["backup"] = str(backup)
        api.write_text_atomic(config, updated)
    return report
