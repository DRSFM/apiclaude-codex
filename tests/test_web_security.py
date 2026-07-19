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
