from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import apiagent
from secure_store import SecureStore
from web.backend import app as web_app


class WebSecurityTests(unittest.TestCase):
    def test_share_api_lists_targets_and_copies_without_credentials(self) -> None:
        targets = [
            {
                "id": "account",
                "label": "账号态 Codex",
                "kind": "account",
                "model": "gpt-test",
                "available": True,
            },
            {
                "id": "api:relay",
                "label": "relay",
                "kind": "api",
                "model": "gpt-relay",
                "available": True,
            },
            {
                "id": "claude:relay",
                "label": "Claude Code: relay",
                "kind": "claude",
                "model": "claude-test",
                "isolation": "isolated",
                "available": True,
            },
        ]
        with patch.object(web_app, "list_share_targets", return_value=targets):
            response = asyncio.run(web_app.list_conversation_targets())
        self.assertEqual(response["targets"], targets)
        self.assertNotIn("api_key", str(response).lower())
        self.assertNotIn("credential", str(response).lower())

        request = web_app.ShareCopyRequest(
            source_target_id="account",
            source_thread_id="thread-source",
            target_target_id="api:relay",
        )
        copied = {
            "ok": True,
            "source": {"targetId": "account", "threadId": "thread-source"},
            "target": {"targetId": "api:relay", "threadId": "thread-target"},
        }
        with patch.object(web_app, "copy_share_thread", return_value=copied):
            response = asyncio.run(web_app.copy_conversation(request))
        self.assertEqual(response["target"]["threadId"], "thread-target")
        self.assertNotIn("api_key", str(response).lower())
        self.assertNotIn("credential", str(response).lower())

    def test_share_frontend_contains_real_migration_controls(self) -> None:
        frontend = (
            Path(web_app.__file__).parent.parent
            / "frontend"
            / "index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('id="migration-view"', frontend)
        self.assertIn('id="migrationSourceTarget"', frontend)
        self.assertIn('id="migrationThreadList"', frontend)
        self.assertIn('id="migrationTargetList"', frontend)
        self.assertIn('id="startMigrationBtn"', frontend)
        self.assertIn("CODEX ↔ CLAUDE CODE SESSION TRANSFER", frontend)

    def test_codex_create_response_and_profile_file_exclude_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex-api"
            profiles_path = codex_home / "profiles.json"
            store = SecureStore(root / "secrets")
            request = web_app.CodexProfileCreate(
                name="relay",
                api_key="sk-test-web-secret",
                base_url="https://example.test/v1",
                model="test-model",
            )

            with (
                patch.object(apiagent, "CODEX_HOME", codex_home),
                patch.object(apiagent, "CODEX_PROFILES_PATH", profiles_path),
                patch.object(apiagent, "CODEX_ARCHIVE_ROOT", codex_home / "archive"),
                patch.object(apiagent, "SECRET_STORE", store),
            ):
                response = asyncio.run(web_app.create_codex_profile(request))

            self.assertNotIn("sk-test-web-secret", str(response))
            self.assertNotIn(
                "sk-test-web-secret",
                profiles_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(store.get("codex:relay"), "sk-test-web-secret")

    def test_claude_create_response_and_config_exclude_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "claude.json"
            store = SecureStore(root / "secrets")
            request = web_app.ClaudeNodeCreate(
                name="relay",
                api_key="sk-test-web-secret",
                base_url="https://example.test",
            )

            with (
                patch.object(apiagent, "CLAUDE_CONFIG_PATH", config_path),
                patch.object(apiagent, "SECRET_STORE", store),
            ):
                response = asyncio.run(web_app.create_claude_node(request))

            self.assertNotIn("sk-test-web-secret", str(response))
            self.assertNotIn(
                "sk-test-web-secret",
                config_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(store.get("claude:relay"), "sk-test-web-secret")


if __name__ == "__main__":
    unittest.main()
