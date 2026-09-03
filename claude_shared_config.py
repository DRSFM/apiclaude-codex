from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ClaudeMcpMergeResult:
    payload: dict[str, Any]
    source_hashes: dict[str, str]
    conflicts: tuple[str, ...]
    changed: bool


def extract_mcp_servers(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("mcpServers")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("mcpServers must contain a JSON object")
    servers: dict[str, dict[str, Any]] = {}
    for name, definition in raw.items():
        if not isinstance(name, str) or not name:
            raise ValueError("MCP server names must be non-empty strings")
        if not isinstance(definition, dict):
            raise ValueError(f"MCP server '{name}' must contain a JSON object")
        servers[name] = definition
    return servers


def mcp_server_hash(definition: dict[str, Any]) -> str:
    normalized = json.dumps(
        definition,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def merge_shared_mcp_servers(
    target_payload: dict[str, Any],
    source_payload: dict[str, Any],
    previous_hashes: dict[str, str] | None = None,
) -> ClaudeMcpMergeResult:
    previous = previous_hashes or {}
    source_servers = extract_mcp_servers(source_payload)
    target_servers = extract_mcp_servers(target_payload)
    source_hashes = {
        name: mcp_server_hash(definition)
        for name, definition in source_servers.items()
    }
    merged_servers = copy.deepcopy(target_servers)
    conflicts: list[str] = []

    for name in dict.fromkeys([*previous, *source_servers]):
        target_definition = target_servers.get(name)
        target_hash = (
            mcp_server_hash(target_definition)
            if target_definition is not None
            else None
        )
        previous_hash = previous.get(name)
        source_definition = source_servers.get(name)
        source_hash = source_hashes.get(name)

        if source_definition is None:
            if target_definition is not None and previous_hash:
                if target_hash == previous_hash:
                    merged_servers.pop(name, None)
                else:
                    conflicts.append(name)
            continue

        if target_definition is None:
            merged_servers[name] = copy.deepcopy(source_definition)
        elif target_hash == source_hash:
            continue
        elif previous_hash and target_hash == previous_hash:
            merged_servers[name] = copy.deepcopy(source_definition)
        else:
            conflicts.append(name)

    merged_payload = copy.deepcopy(target_payload)
    if merged_servers or "mcpServers" in target_payload or source_servers:
        merged_payload["mcpServers"] = merged_servers
    return ClaudeMcpMergeResult(
        payload=merged_payload,
        source_hashes=source_hashes,
        conflicts=tuple(conflicts),
        changed=merged_payload != target_payload,
    )
