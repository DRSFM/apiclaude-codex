"""Plan and apply account-to-node sharing of Claude instructions and skills.

``apiagent`` owns the opt-in state and decides which account paths may be read.
This module only compares and copies what it is handed, so the explicit
``--account`` authorization stays in one place.

Two rules hold for every operation here:

* Node content this tool did not put there is never overwritten or removed. It
  is reported as a conflict and left alone.
* A skill is shared as a directory link. Only links are ever removed; the
  directory a link points at is never touched.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


INSTRUCTIONS_FILE_NAME = "CLAUDE.md"
SKILLS_DIR_NAME = "skills"
SKILL_MANIFEST_NAME = "SKILL.md"
_JUNCTION_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)

# (label shown to the user, node config directory)
Target = tuple[str, Path]


@dataclass(frozen=True)
class InstructionsPlan:
    source_hash: str | None
    writes: tuple[tuple[str, Path], ...]
    removes: tuple[tuple[str, Path], ...]
    conflicts: tuple[str, ...]
    managed: dict[str, str]


@dataclass(frozen=True)
class SkillsPlan:
    skills: tuple[str, ...]
    links: tuple[tuple[str, Path, Path], ...]
    relinks: tuple[tuple[str, Path, Path], ...]
    unlinks: tuple[tuple[str, Path], ...]
    conflicts: dict[str, tuple[str, ...]]
    managed: dict[str, str]


def path_key(path: Path | str) -> str:
    """Return a state key for ``path`` that ignores Windows case and prefixes."""
    text = os.path.abspath(str(path))
    if text.startswith("\\\\?\\") and not text.startswith("\\\\?\\UNC\\"):
        text = text[4:]
    return os.path.normcase(text)


def _same_path(left: Path | str, right: Path | str) -> bool:
    return path_key(left) == path_key(right)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def plan_shared_instructions(
    source: bytes | None,
    targets: Sequence[Target],
    managed: Mapping[str, str],
) -> InstructionsPlan:
    """Decide which node copies of the account instructions to write or remove.

    ``source`` is the account file's content, or None when sharing is being
    turned off. ``managed`` maps each node copy to the hash last written there;
    a copy whose hash no longer matches was edited in the node and is kept.
    Entries for nodes outside ``targets`` pass through unchanged.
    """
    source_hash = _sha256(source) if source is not None else None
    next_managed = dict(managed)
    writes: list[tuple[str, Path]] = []
    removes: list[tuple[str, Path]] = []
    conflicts: list[str] = []

    for label, home in targets:
        path = home / INSTRUCTIONS_FILE_NAME
        key = path_key(path)
        previous_hash = managed.get(key)
        existing = _read_bytes(path)
        existing_hash = _sha256(existing) if existing is not None else None

        if source_hash is None:
            if existing_hash is not None and previous_hash:
                if existing_hash == previous_hash:
                    removes.append((label, path))
                else:
                    conflicts.append(label)
            next_managed.pop(key, None)
            continue

        if existing_hash == source_hash:
            next_managed[key] = source_hash
        elif existing_hash is None or existing_hash == previous_hash:
            writes.append((label, path))
            next_managed[key] = source_hash
        else:
            conflicts.append(label)

    return InstructionsPlan(
        source_hash=source_hash,
        writes=tuple(writes),
        removes=tuple(removes),
        conflicts=tuple(conflicts),
        managed=next_managed,
    )


def apply_shared_instructions(plan: InstructionsPlan, source: bytes | None) -> None:
    if plan.writes and source is None:
        raise ValueError("instruction copies cannot be written without a source")
    for _label, path in plan.writes:
        write_bytes_atomic(path, source or b"")
    for _label, path in plan.removes:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def directory_link_target(path: Path) -> Path | None:
    """Return where ``path`` points if it is a symlink or junction, else None."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    is_link = stat.S_ISLNK(info.st_mode) or (
        getattr(info, "st_reparse_tag", 0) == _JUNCTION_TAG
    )
    if not is_link:
        return None
    return Path(os.path.realpath(path))


def create_directory_link(link: Path, target: Path) -> None:
    """Create ``link`` as a junction (Windows) or symlink pointing at ``target``."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    try:
        import _winapi

        create_junction = _winapi.CreateJunction
    except (ImportError, AttributeError):
        create_junction = None
    if create_junction is not None:
        try:
            create_junction(str(target), str(link))
        except OSError:
            # A failed attempt can leave the empty directory it started from.
            if os.path.isdir(link) and directory_link_target(link) is None:
                try:
                    os.rmdir(link)
                except OSError:
                    pass
            raise
        return
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise OSError(f"could not create junction {link}: {detail}")


def remove_directory_link(link: Path) -> None:
    """Remove a symlink or junction without touching what it points at."""
    if directory_link_target(link) is None:
        raise OSError(f"refusing to remove {link}: it is not a directory link")
    if sys.platform == "win32":
        try:
            os.rmdir(link)
            return
        except OSError:
            pass
    os.unlink(link)


def discover_shared_skills(skills_dir: Path) -> dict[str, Path]:
    """Map each account skill name to the real directory that holds it.

    Hidden entries and directories without ``SKILL.md`` are not skills and are
    skipped. Raises ``FileNotFoundError`` when ``skills_dir`` does not exist.
    """
    skills: dict[str, Path] = {}
    for entry in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
        if entry.name.startswith("."):
            continue
        real = Path(os.path.realpath(entry))
        if (real / SKILL_MANIFEST_NAME).is_file():
            skills[entry.name] = real
    return skills


def plan_shared_skills(
    skills: Mapping[str, Path] | None,
    targets: Sequence[Target],
    managed: Mapping[str, str],
) -> SkillsPlan:
    """Decide which node skill links to create, repoint, or remove.

    ``skills`` is None when sharing is being turned off. ``managed`` maps each
    link this tool created to the directory it pointed at. A node entry that is
    a real directory, or a link created by someone else, always stays as it is.
    Entries for nodes outside ``targets`` pass through unchanged.
    """
    source = dict(skills or {})
    next_managed = dict(managed)
    links: list[tuple[str, Path, Path]] = []
    relinks: list[tuple[str, Path, Path]] = []
    unlinks: list[tuple[str, Path]] = []
    conflicts: dict[str, list[str]] = {}

    for label, home in targets:
        skills_root = home / SKILLS_DIR_NAME
        wanted = {path_key(skills_root / name): name for name in source}

        prefix = path_key(skills_root) + os.sep
        for key, recorded in managed.items():
            if not key.startswith(prefix) or key in wanted:
                continue
            link = Path(key)
            current = directory_link_target(link)
            if current is not None and _same_path(current, recorded):
                unlinks.append((label, link))
            next_managed.pop(key, None)

        for key, name in wanted.items():
            link = skills_root / name
            real = source[name]
            recorded = managed.get(key)
            if not os.path.lexists(link):
                links.append((label, link, real))
                next_managed[key] = str(real)
                continue
            current = directory_link_target(link)
            if current is None:
                next_managed.pop(key, None)
                # A real directory: either the shared skill itself, or node-local.
                if not _same_path(os.path.realpath(link), real):
                    conflicts.setdefault(label, []).append(name)
            elif _same_path(current, real):
                next_managed[key] = str(real)
            elif recorded and _same_path(current, recorded):
                relinks.append((label, link, real))
                next_managed[key] = str(real)
            else:
                next_managed.pop(key, None)
                conflicts.setdefault(label, []).append(name)

    return SkillsPlan(
        skills=tuple(source),
        links=tuple(links),
        relinks=tuple(relinks),
        unlinks=tuple(unlinks),
        conflicts={label: tuple(names) for label, names in conflicts.items()},
        managed=next_managed,
    )


def _ensure_directory_link(link: Path, target: Path) -> None:
    try:
        create_directory_link(link, target)
    except OSError:
        # Another launch may have created the same link a moment ago.
        current = directory_link_target(link)
        if current is None or not _same_path(current, target):
            raise


def apply_shared_skills(plan: SkillsPlan) -> None:
    for _label, link in plan.unlinks:
        if os.path.lexists(link):
            remove_directory_link(link)
    for _label, link, target in plan.relinks:
        if os.path.lexists(link):
            remove_directory_link(link)
        _ensure_directory_link(link, target)
    for _label, link, target in plan.links:
        _ensure_directory_link(link, target)
