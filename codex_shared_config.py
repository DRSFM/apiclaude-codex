from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


PROFILE_OWNED_MCP_SERVERS = frozenset({"cua_repl", "node_repl"})
PROFILE_OWNED_MCP_PREFIXES = ("apicodex_",)
_TABLE_HEADER_RE = re.compile(r"^\s*\[(?!\[)(.*)\]\s*(?:#.*)?$")


@dataclass(frozen=True)
class McpMergeResult:
    text: str
    source_hashes: dict[str, str]
    conflicts: tuple[str, ...]
    excluded: tuple[str, ...]
    changed: bool


def _decode_toml_key(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    if token.startswith('"'):
        if not token.endswith('"'):
            return None
        try:
            value = json.loads(token)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return value if isinstance(value, str) else None
    if token.startswith("'"):
        if not token.endswith("'"):
            return None
        return token[1:-1]
    return token


def _split_toml_dotted_key(value: str) -> tuple[str, ...] | None:
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    escaped = False
    for char in value:
        if quote is not None:
            current.append(char)
            if quote == '"' and escaped:
                escaped = False
            elif quote == '"' and char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            current.append(char)
        elif char == ".":
            decoded = _decode_toml_key("".join(current))
            if decoded is None:
                return None
            parts.append(decoded)
            current = []
        else:
            current.append(char)
    if quote is not None:
        return None
    decoded = _decode_toml_key("".join(current))
    if decoded is None:
        return None
    parts.append(decoded)
    return tuple(parts)


def _table_spans(text: str) -> tuple[list[str], list[tuple[int, int, tuple[str, ...] | None]]]:
    lines = text.splitlines(keepends=True)
    headers: list[tuple[int, tuple[str, ...] | None]] = []
    for index, line in enumerate(lines):
        match = _TABLE_HEADER_RE.match(line.rstrip("\r\n"))
        if match:
            headers.append((index, _split_toml_dotted_key(match.group(1))))
    spans = [
        (start, headers[index + 1][0] if index + 1 < len(headers) else len(lines), parts)
        for index, (start, parts) in enumerate(headers)
    ]
    return lines, spans


def extract_mcp_server_sections(text: str) -> dict[str, str]:
    lines, spans = _table_spans(text)
    grouped: dict[str, list[str]] = {}
    for start, end, parts in spans:
        if not parts or len(parts) < 2 or parts[0] != "mcp_servers":
            continue
        block = "".join(lines[start:end]).strip("\r\n")
        grouped.setdefault(parts[1], []).append(block)
    return {
        name: "\n\n".join(part for part in blocks if part)
        for name, blocks in grouped.items()
    }


def mcp_section_hash(section: str) -> str:
    normalized = section.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_profile_owned_mcp_server(name: str) -> bool:
    return name in PROFILE_OWNED_MCP_SERVERS or name.startswith(
        PROFILE_OWNED_MCP_PREFIXES
    )


def _remove_mcp_server_sections(text: str, names: set[str]) -> str:
    if not names:
        return text
    lines, spans = _table_spans(text)
    remove_lines: set[int] = set()
    for start, end, parts in spans:
        if parts and len(parts) >= 2 and parts[0] == "mcp_servers" and parts[1] in names:
            remove_lines.update(range(start, end))
    return "".join(line for index, line in enumerate(lines) if index not in remove_lines)


def append_toml_sections(text: str, sections: list[str]) -> str:
    if not sections:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    rendered = text.rstrip("\r\n")
    if rendered:
        rendered += newline + newline
    normalized = [
        section.replace("\r\n", "\n").replace("\r", "\n").strip().replace(
            "\n", newline
        )
        for section in sections
    ]
    rendered += (newline + newline).join(normalized)
    return rendered + newline


def preserve_mcp_server_sections(base_text: str, existing_text: str) -> str:
    sections = list(extract_mcp_server_sections(existing_text).values())
    return append_toml_sections(base_text, sections)


def merge_shared_mcp_servers(
    target_text: str,
    source_text: str,
    previous_hashes: dict[str, str] | None = None,
) -> McpMergeResult:
    previous = previous_hashes or {}
    source_sections_all = extract_mcp_server_sections(source_text)
    excluded = tuple(
        name for name in source_sections_all if is_profile_owned_mcp_server(name)
    )
    source_sections = {
        name: section
        for name, section in source_sections_all.items()
        if name not in excluded
    }
    source_hashes = {
        name: mcp_section_hash(section) for name, section in source_sections.items()
    }
    target_sections = extract_mcp_server_sections(target_text)
    remove: set[str] = set()
    append: list[str] = []
    conflicts: list[str] = []

    for name in dict.fromkeys([*previous, *source_sections]):
        target_section = target_sections.get(name)
        target_hash = mcp_section_hash(target_section) if target_section else None
        previous_hash = previous.get(name)
        source_section = source_sections.get(name)
        source_hash = source_hashes.get(name)

        if source_section is None:
            if target_section is not None and previous_hash:
                if target_hash == previous_hash:
                    remove.add(name)
                else:
                    conflicts.append(name)
            continue

        if target_section is None:
            append.append(source_section)
        elif target_hash == source_hash:
            continue
        elif previous_hash and target_hash == previous_hash:
            remove.add(name)
            append.append(source_section)
        else:
            conflicts.append(name)

    merged = append_toml_sections(
        _remove_mcp_server_sections(target_text, remove),
        append,
    )
    return McpMergeResult(
        text=merged,
        source_hashes=source_hashes,
        conflicts=tuple(conflicts),
        excluded=excluded,
        changed=merged != target_text,
    )
