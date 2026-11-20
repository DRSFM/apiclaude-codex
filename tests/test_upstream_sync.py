from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

import apiagent as api
import upstream_sync as sync
from codex_accounts import registry_profiles


class MemoryStore:
    def __init__(self):
        self.keys = {}

    def get(self, reference):
        return self.keys[reference]

    def set(self, reference, value):
        self.keys[reference] = value

    def clear(self, reference):
        self.keys.pop(reference, None)


@contextmanager
def device(root, store=None):
    store = store if store is not None else MemoryStore()
    codex_home = root / ".codex-api"
    with patch.multiple(
        api,
        HOME=root,
        CODEX_HOME=codex_home,
        CODEX_PROFILES_PATH=codex_home / "profiles.json",
        CLAUDE_CONFIG_PATH=root / ".apiclaude_config.json",
        CLAUDE_NODES_ROOT=root / ".apiclaude",
        SECRET_STORE=store,
    ):
        yield store


def create_source():
    api.SECRET_STORE.keys.update({
        "codex-1": "synthetic-codex-one",
        "codex-2": "synthetic-codex-two",
        "claude-1": "synthetic-claude-one",
    })
    api.save_codex_profiles([
        {
            "id": "anyrouter", "name": "AnyRouter", "home": "profiles/anyrouter",
            "baseUrl": "https://relay.test/v1", "model": "gpt-test",
            "reasoningEffort": "high", "credentialId": "codex-2",
            "apiKeys": [
                {"name": "account-1", "credentialId": "codex-1"},
                {"name": "account-2", "credentialId": "codex-2"},
            ],
        },
        {"id": "official", "name": "Official", "type": "chatgpt"},
        {"id": "localhost", "name": "Local", "baseUrl": "http://localhost:1234/v1"},
    ])
    api.save_claude_config({
        "current": "AnyRouter",
        "nodes": {
            "AnyRouter": {
                "base_url": "https://relay.test", "credential_id": "claude-1",
                "isolation": "shared", "proxy_url": "http://127.0.0.1:7890",
                "desktop_models": ["claude-test"],
            },
            "Bridge": {"type": "codex_bridge", "codex_profile": "anyrouter"},
        },
    })


def test_copy_only_upstreams_and_use_destination_secure_store(tmp_path):
    with device(tmp_path / "source"):
        create_source()
        entries = sync.collect(api, [])
        assert [(entry["family"], entry["name"]) for entry in entries] == [
            ("codex", "AnyRouter"), ("claude", "AnyRouter"),
        ]
        assert len(sync.collect(api, ["ANYROUTER"])) == 2

    root = tmp_path / "destination"
    with device(root) as store:
        assert sync.apply(api, entries, dry_run=True, update_keys=False) == 2
        assert not root.exists()
        assert store.keys == {}
        assert sync.apply(api, entries, dry_run=False, update_keys=False) == 2
        profile, = registry_profiles(api)
        node = api._read_claude_config()["nodes"]["AnyRouter"]
        assert api.get_codex_secret(profile) == "synthetic-codex-two"
        assert store.get(profile["apiKeys"][0]["credentialId"]) == "synthetic-codex-one"
        assert api.get_claude_secret("AnyRouter", node) == "synthetic-claude-one"
        assert node["isolation"] == "isolated"
        assert not node["proxy_enabled"]
        assert node["desktop_models"] == ["claude-test"]
        config_text = (api.codex_profile_home(profile) / "config.toml").read_text()
        assert 'base_url = "https://relay.test/v1"' in config_text
        assert 'model_reasoning_effort = "high"' in config_text
        for path in (api.CODEX_PROFILES_PATH, api.CLAUDE_CONFIG_PATH):
            data = path.read_text(encoding="utf-8")
            assert "synthetic-" not in data
            assert "127.0.0.1:7890" not in data
        before = (api.CODEX_PROFILES_PATH.read_bytes(), dict(store.keys))
        assert sync.apply(api, entries, dry_run=False, update_keys=False) == 0
        assert (api.CODEX_PROFILES_PATH.read_bytes(), store.keys) == before


def test_update_retains_local_config_and_current_key_choice(tmp_path):
    with device(tmp_path / "source"):
        create_source()
        entries = sync.collect(api, [])

    with device(tmp_path / "destination") as store:
        sync.apply(api, entries, dry_run=False, update_keys=False)
        profile, = registry_profiles(api)
        profile["credentialId"] = profile["apiKeys"][0]["credentialId"]
        profile["model"] = "device-local-model"
        api.save_codex_profiles([profile])
        configured = api.CODEX_HOME / profile["home"] / "config.toml"
        before = configured.read_bytes()
        entries[0]["keys"]["account-1"] = "synthetic-rotated-codex"
        entries[0]["base_url"] = "https://different.test/v1"
        assert sync.apply(api, entries, dry_run=False, update_keys=True) == 0
        assert api.get_codex_secret(registry_profiles(api)[0]) == "synthetic-codex-one"
        entries[0]["base_url"] = "https://relay.test/v1"
        assert sync.apply(api, entries, dry_run=False, update_keys=True) == 1
        updated, = registry_profiles(api)
        assert updated["model"] == "device-local-model"
        assert updated["credentialId"] == updated["apiKeys"][0]["credentialId"]
        assert api.get_codex_secret(updated) == "synthetic-rotated-codex"
        assert configured.read_bytes() == before
        assert len(store.keys) == 3
        assert sync.apply(api, entries, dry_run=False, update_keys=True) == 0


def test_failed_registry_write_keeps_existing_credentials(tmp_path):
    with device(tmp_path / "source"):
        create_source()
        entries = sync.collect(api, [])
    with device(tmp_path / "destination") as store:
        sync.apply(api, entries, dry_run=False, update_keys=False)
        before = dict(store.keys)
        entries[0]["keys"]["account-2"] = "synthetic-new-key"
        with patch.object(api, "save_codex_profiles", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError, match="simulated failure"):
                sync.apply(api, entries, dry_run=False, update_keys=True)
        assert store.keys == before
        assert api.get_codex_secret(registry_profiles(api)[0]) == "synthetic-codex-two"


def test_encrypted_bundle_round_trip_with_both_entry_points(tmp_path, capsys):
    pytest.importorskip("cryptography")
    bundle = tmp_path / "test.apisync"
    passphrase = "a synthetic test passphrase"
    with device(tmp_path / "source"):
        create_source()
        with patch.object(api, "getpass", return_value=passphrase), patch.object(sync, "_send") as sender:
            assert api.codex_main([
                "sync", "export", str(bundle), "--name", "anyrouter",
                "--to", "beyonder", "--to", "macbook-air",
            ]) == 0
            sender.assert_called_once_with(bundle, ["beyonder", "macbook-air"])
    ciphertext = bundle.read_bytes()
    assert ciphertext.startswith(sync.BUNDLE_MAGIC)
    assert b"synthetic-codex" not in ciphertext
    assert b"relay.test" not in ciphertext
    assert passphrase.encode() not in ciphertext
    with device(tmp_path / "destination") as store:
        with patch.object(api, "getpass", return_value="incorrect passphrase"):
            assert api.claude_main(["sync", "import", str(bundle)]) == 1
        assert not store.keys
        with patch.object(api, "getpass", return_value=passphrase):
            bundle.write_bytes(ciphertext[:-1] + bytes([ciphertext[-1] ^ 1]))
            assert api.claude_main(["sync", "import", str(bundle)]) == 1
            assert not store.keys
            bundle.write_bytes(ciphertext)
            assert api.claude_main(["sync", "import", str(bundle), "--dry-run"]) == 0
            assert not store.keys
            assert api.apiagent_main(["sync", "import", str(bundle)]) == 0
        assert len(store.keys) == 3
    captured = capsys.readouterr()
    assert "Could not unlock" in captured.err
    assert "synthetic-" not in captured.out + captured.err


def test_export_does_not_overwrite_existing_bundle(tmp_path):
    bundle = tmp_path / "test.apisync"
    bundle.write_bytes(b"existing")
    with device(tmp_path / "source"), patch.object(api, "getpass") as prompt:
        assert api.apiagent_main(["sync", "export", str(bundle)]) == 1
    prompt.assert_not_called()
    assert bundle.read_bytes() == b"existing"


def test_numbered_menu_exports_only_selected_upstream(tmp_path, capsys):
    pytest.importorskip("cryptography")
    phrase = "a synthetic menu passphrase"
    with device(tmp_path / "source") as store:
        create_source()
        with (
            patch.object(sync.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=["99", "2", "2，1,2"]),
            patch.object(sync, "_taildrop_targets", return_value=["beyonder", "macbook-air"]),
            patch.object(sync, "_send") as sender,
            patch.object(api, "getpass", return_value=phrase),
            patch.object(store, "get", wraps=store.get) as read_key,
        ):
            assert api.codex_main(["sync"]) == 0
        bundle, targets = sender.call_args.args
        assert targets == ["macbook-air", "beyonder"]
        assert bundle.parent == api.HOME / ".apiagent-sync"
        entries = sync.unseal(bundle.read_bytes(), phrase.encode())
        assert [(entry["family"], entry["name"]) for entry in entries] == [("claude", "AnyRouter")]
        read_key.assert_called_once_with("claude-1")
    output = capsys.readouterr().out
    assert "1. ApiCodex / AnyRouter" in output
    assert "2. ApiClaude / AnyRouter" in output
    assert "2. macbook-air" in output
    assert "synthetic-claude-one" not in output


@pytest.mark.parametrize("inputs", [("q",), ("1", "q")])
def test_numbered_menu_cancel_leaves_keys_unread(tmp_path, inputs):
    with device(tmp_path / "source") as store:
        create_source()
        with (
            patch.object(sync.sys.stdin, "isatty", return_value=True),
            patch("builtins.input", side_effect=inputs),
            patch.object(sync, "_taildrop_targets", return_value=["beyonder"]),
            patch.object(sync, "_send") as sender,
            patch.object(api, "getpass") as passphrase,
            patch.object(store, "get") as read_key,
        ):
            assert api.claude_main(["sync"]) == 0
        assert not (api.HOME / ".apiagent-sync").exists()
        sender.assert_not_called()
        passphrase.assert_not_called()
        read_key.assert_not_called()


def test_taildrop_menu_lists_online_targets():
    output = (
        "100.101.102.103\tbeyonder\n"
        "100.101.102.104\tmacbook-air\n"
        "100.101.102.105\tsfm\toffline; last seen 2h ago\n"
        "unexpected line of status text\n"
    )
    with (
        patch.object(sync, "_tailscale_client", return_value=("tailscale", None)),
        patch.object(sync.subprocess, "run") as run,
    ):
        run.return_value.returncode = 0
        run.return_value.stdout = output
        assert sync._taildrop_targets() == ["beyonder", "macbook-air"]
    assert run.call_args.args[0] == ["tailscale", "file", "cp", "--targets"]
