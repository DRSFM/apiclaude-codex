from __future__ import annotations

import unittest

from codex_shared_config import (
    extract_mcp_server_sections,
    merge_shared_mcp_servers,
    preserve_mcp_server_sections,
)


class CodexSharedConfigTests(unittest.TestCase):
    def test_extracts_nested_and_quoted_mcp_server_tables(self) -> None:
        config = (
            'model = "test"\n\n'
            '[mcp_servers.fetch]\n'
            'command = "fetch"\n\n'
            '[mcp_servers.fetch.env]\n'
            'MODE = "safe"\n\n'
            '[mcp_servers."docs.search"]\n'
            'url = "https://example.test/mcp"\n'
        )

        sections = extract_mcp_server_sections(config)

        self.assertEqual(list(sections), ["fetch", "docs.search"])
        self.assertIn("[mcp_servers.fetch.env]", sections["fetch"])
        self.assertIn('url = "https://example.test/mcp"', sections["docs.search"])

    def test_merge_adds_shared_servers_and_excludes_profile_owned_servers(self) -> None:
        target = (
            'model = "test"\n\n'
            '[mcp_servers.profile_search]\n'
            'command = "profile-search"\n'
        )
        source = (
            '[mcp_servers.fetch]\n'
            'command = "fetch"\n\n'
            '[mcp_servers.node_repl]\n'
            'command = "node"\n\n'
            '[mcp_servers.apicodex_vision]\n'
            'command = "vision"\n'
        )

        result = merge_shared_mcp_servers(target, source)

        self.assertTrue(result.changed)
        self.assertEqual(list(result.source_hashes), ["fetch"])
        self.assertEqual(result.excluded, ("node_repl", "apicodex_vision"))
        self.assertIn('[mcp_servers.fetch]\ncommand = "fetch"', result.text)
        self.assertIn("[mcp_servers.profile_search]", result.text)
        self.assertNotIn("[mcp_servers.node_repl]", result.text)

    def test_merge_updates_and_removes_unchanged_managed_servers(self) -> None:
        first_source = '[mcp_servers.fetch]\ncommand = "fetch-v1"\n'
        first = merge_shared_mcp_servers('model = "test"\n', first_source)
        second_source = '[mcp_servers.fetch]\ncommand = "fetch-v2"\n'

        second = merge_shared_mcp_servers(
            first.text,
            second_source,
            first.source_hashes,
        )
        removed = merge_shared_mcp_servers(
            second.text,
            "",
            second.source_hashes,
        )

        self.assertIn('command = "fetch-v2"', second.text)
        self.assertNotIn('command = "fetch-v1"', second.text)
        self.assertNotIn("[mcp_servers.fetch]", removed.text)

    def test_merge_preserves_profile_local_edits_as_conflicts(self) -> None:
        source = '[mcp_servers.fetch]\ncommand = "shared-fetch"\n'
        first = merge_shared_mcp_servers('model = "test"\n', source)
        locally_edited = first.text.replace("shared-fetch", "profile-fetch")

        result = merge_shared_mcp_servers(
            locally_edited,
            '[mcp_servers.fetch]\ncommand = "shared-fetch-v2"\n',
            first.source_hashes,
        )

        self.assertEqual(result.conflicts, ("fetch",))
        self.assertIn('command = "profile-fetch"', result.text)
        self.assertNotIn("shared-fetch-v2", result.text)

    def test_preserve_mcp_sections_keeps_profile_servers_on_config_rewrite(self) -> None:
        base = 'model = "new"\n\n[features]\napps = false\n'
        existing = (
            'model = "old"\n\n'
            '[mcp_servers.local]\n'
            'command = "local-mcp"\n'
        )

        result = preserve_mcp_server_sections(base, existing)

        self.assertIn('model = "new"', result)
        self.assertIn('[mcp_servers.local]\ncommand = "local-mcp"', result)


if __name__ == "__main__":
    unittest.main()
