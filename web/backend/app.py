#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI backend for API node management web interface.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

# Add parent directory to path to import apiagent
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
import apiagent

app = FastAPI(title="API Node Manager", version="1.0.0")

# CORS middleware for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
frontend_dir = Path(__file__).parent.parent / "frontend"
app.mount("/static", StaticFiles(directory=frontend_dir / "static"), name="static")


class CodexProfileCreate(BaseModel):
    name: str
    api_key: str
    base_url: str | None = None
    model: str | None = None


class ClaudeNodeCreate(BaseModel):
    name: str
    api_key: str
    base_url: str | None = None


class LaunchRequest(BaseModel):
    args: list[str] = []


class ClaudeStartRequest(BaseModel):
    folder: str
    mode: str  # "new" or "resume"
    permission: str  # "default", "bypassPermissions", "sandbox"
    model: str | None = None  # "claude-opus-4-6", etc.


class CodexStartRequest(BaseModel):
    folder: str


executor = ThreadPoolExecutor(max_workers=2)


@app.get("/")
async def root():
    return FileResponse(frontend_dir / "index.html")


@app.get("/api/codex/profiles")
async def list_codex_profiles():
    profiles = apiagent.load_codex_profiles()
    return {"profiles": profiles}


@app.post("/api/codex/profiles")
async def create_codex_profile(profile: CodexProfileCreate):
    profiles = apiagent.load_codex_profiles()

    slug = apiagent.slugify(profile.name)
    if any(p.get("name") == slug for p in profiles):
        raise HTTPException(status_code=400, detail=f"Profile '{slug}' already exists")

    cleaned_key = apiagent.clean_hidden_prefix(profile.api_key)
    base_url = profile.base_url or apiagent.DEFAULT_CODEX_BASE_URL
    model = profile.model or apiagent.DEFAULT_CODEX_MODEL

    new_profile = {
        "name": slug,
        "home": slug,
        "api_key": cleaned_key,
        "base_url": base_url,
        "model": model,
        "created_at": apiagent.now_iso(),
        "updated_at": apiagent.now_iso(),
    }

    home = apiagent.CODEX_HOME / slug
    home.mkdir(parents=True, exist_ok=True)
    config_path = home / "config.toml"
    config_path.write_text(
        f'base_url = {apiagent.toml_basic_string(base_url)}\n'
        f'model = {apiagent.toml_basic_string(model)}\n',
        encoding="utf-8"
    )

    profiles.append(new_profile)
    apiagent.save_codex_profiles(profiles)

    return {"profile": new_profile, "message": "Profile created"}


@app.delete("/api/codex/profiles/{name}")
async def delete_codex_profile(name: str):
    profiles = apiagent.load_codex_profiles()
    match = next((p for p in profiles if p.get("name") == name), None)

    if not match:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    profiles.remove(match)
    apiagent.save_codex_profiles(profiles)

    home = apiagent.codex_profile_home(match)
    if home.exists() and home != apiagent.CODEX_HOME:
        archive_dir = apiagent.CODEX_ARCHIVE_ROOT / f"{name}-{apiagent.now_iso().replace(':', '-')}"
        archive_dir.parent.mkdir(parents=True, exist_ok=True)
        home.rename(archive_dir)

    return {"message": f"Profile '{name}' removed"}


@app.get("/api/claude/nodes")
async def list_claude_nodes():
    config = apiagent.load_claude_config()
    nodes = config.get("nodes") or {}
    current = config.get("current")
    safe_nodes = {}
    for name, node in nodes.items():
        safe_nodes[name] = {
            "base_url": node.get("base_url", ""),
            "token": apiagent.mask_secret(node.get("token", "")),
        }
    return {"nodes": safe_nodes, "current": current}


@app.post("/api/claude/nodes")
async def create_claude_node(node: ClaudeNodeCreate):
    config = apiagent.load_claude_config()
    nodes = config.setdefault("nodes", {})

    if node.name in nodes:
        raise HTTPException(status_code=400, detail=f"Node '{node.name}' already exists")

    cleaned_token = apiagent.clean_hidden_prefix(node.api_key)
    base_url = apiagent.clean_hidden_prefix(node.base_url or "")

    nodes[node.name] = {"base_url": base_url, "token": cleaned_token}
    if not config.get("current"):
        config["current"] = node.name
    apiagent.save_claude_config(config)

    return {"node": nodes[node.name], "message": f"Node '{node.name}' created"}


@app.delete("/api/claude/nodes/{name}")
async def delete_claude_node(name: str):
    config = apiagent.load_claude_config()
    nodes = config.get("nodes") or {}

    if name not in nodes:
        raise HTTPException(status_code=404, detail=f"Node '{name}' not found")

    del nodes[name]
    if config.get("current") == name:
        config["current"] = None
    apiagent.save_claude_config(config)

    return {"message": f"Node '{name}' removed"}


@app.post("/api/codex/launch/{name}")
async def launch_codex(name: str, request: LaunchRequest):
    profiles = apiagent.load_codex_profiles()
    match = next((p for p in profiles if p.get("name") == name), None)

    if not match:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    return {
        "message": f"Codex launch with profile '{name}' requested",
        "note": "Launch must be handled by desktop app or user terminal"
    }


@app.post("/api/claude/launch/{name}")
async def launch_claude(name: str, request: LaunchRequest):
    config = apiagent.load_claude_config()
    nodes = config.get("nodes") or {}

    if name not in nodes:
        raise HTTPException(status_code=404, detail=f"Node '{name}' not found")

    config["current"] = name
    apiagent.save_claude_config(config)

    return {
        "message": f"Claude node switched to '{name}'",
        "note": "Launch must be handled by desktop app or user terminal"
    }


def _browse_folder_sync():
    """Synchronous folder picker using tkinter."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        folder = filedialog.askdirectory(title="选择项目文件夹")
        root.destroy()
        return folder or None
    except Exception:
        return None


@app.post("/api/browse-folder")
async def browse_folder():
    """Open system folder picker dialog."""
    import asyncio
    loop = asyncio.get_event_loop()
    folder = await loop.run_in_executor(executor, _browse_folder_sync)
    return {"folder": folder}


@app.post("/api/claude/start/{name}")
async def start_claude(name: str, request: ClaudeStartRequest):
    """Launch Claude CLI in a new terminal with selected node."""
    config = apiagent.load_claude_config()
    nodes = config.get("nodes") or {}

    if name not in nodes:
        raise HTTPException(status_code=404, detail=f"Node '{name}' not found")

    folder = request.folder
    mode = request.mode
    permission = request.permission
    model = request.model

    # Switch current node first
    config["current"] = name
    apiagent.save_claude_config(config)

    # Build args for apiclaude run (uses current node)
    args = ["run"]

    # Model should come first (before resume)
    if model:
        args.extend(["--model", model])

    if mode == "resume":
        args.append("resume")

    if permission == "bypassPermissions":
        args.extend(["--permission-mode", "bypassPermissions"])
    elif permission == "sandbox":
        args.extend(["--permission-mode", "sandbox"])

    # Detect terminal
    use_wt = shutil.which("wt") is not None

    if use_wt:
        # Windows Terminal
        cmd = ["wt", "-d", folder, "--", "cmd", "/k", f"apiclaude {' '.join(args)}"]
    else:
        # Fallback to cmd.exe
        cmd = ["cmd", "/c", "start", "cmd", "/k", f"cd /d \"{folder}\" && apiclaude {' '.join(args)}"]

    try:
        subprocess.Popen(cmd, shell=False)
        return {"message": f"Claude launched in {folder}", "success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to launch: {str(e)}")


@app.post("/api/codex/start/{name}")
async def start_codex(name: str, request: CodexStartRequest):
    """Launch Codex CLI in a new terminal with selected profile."""
    profiles = apiagent.load_codex_profiles()
    match = next((p for p in profiles if p.get("name") == name), None)

    if not match:
        raise HTTPException(status_code=404, detail=f"Profile '{name}' not found")

    folder = request.folder

    # Build args for apicodex with --api-profile
    args = ["--api-profile", name]

    use_wt = shutil.which("wt") is not None

    if use_wt:
        cmd = ["wt", "-d", folder, "--", "cmd", "/k", f"apicodex {' '.join(args)}"]
    else:
        cmd = ["cmd", "/c", "start", "cmd", "/k", f"cd /d \"{folder}\" && apicodex {' '.join(args)}"]

    try:
        subprocess.Popen(cmd, shell=False)
        return {"message": f"Codex launched in {folder}", "success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to launch: {str(e)}")

