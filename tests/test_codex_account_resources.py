from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apiagent
import codex_account_resources as resources


class SharedResourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / '.codex'
        self.home = self.root / 'accounts' / 'stable'
        self.source.mkdir()
        self.home.mkdir(parents=True)
        self.original = 'model = "local-model"\nforced_login_method = "chatgpt"\ncli_auth_credentials_store = "keyring"\nmodel_provider = "openai"\n\n[history]\npersistence = "save-all"\n'
        (self.home / 'config.toml').write_text(self.original)
        (self.source / 'config.toml').write_text('model = "default-model"\nmodel_provider = "other"\ncli_auth_credentials_store = "file"\n\n[mcp_servers.shared]\ncommand = "shared-tool"\n\n[plugins."tool@test"]\nenabled = true\n')

    def test_config_follows_source_without_sharing_identity_model_or_history(self):
        source = (self.source / 'config.toml').read_text()
        merged = resources.merge_config(self.original, source)
        self.assertIn('model = "local-model"', merged)
        self.assertIn('model_provider = "openai"', merged)
        self.assertIn('cli_auth_credentials_store = "keyring"', merged)
        self.assertIn('[history]', merged)
        self.assertIn('[mcp_servers.shared]', merged)
        self.assertIn('[plugins."tool@test"]', merged)
        self.assertNotIn('default-model', merged)
        self.assertNotIn('"file"', merged)
        self.assertEqual(resources.merge_config(merged, source), merged)

    def test_multiline_values_array_tables_and_top_level_order(self):
        source = '''marketplaces = [
  "/shared/plugins",
]
notify = ["helper", "--notify"]
[mcp_servers.shared]
command = "helper"
description = """
[model_providers.fake]
This must stay inside the description.
"""
[[skills.config]]
path = "/shared/skills/a/SKILL.md"
enabled = false
[features]
js_repl = true
secret_auth_storage = false
'''
        merged = resources.merge_config(self.original, source)
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest('TOML semantic validation requires Python 3.11+')
        data = tomllib.loads(merged)
        self.assertEqual(data['marketplaces'], ['/shared/plugins'])
        self.assertEqual(data['model'], 'local-model')
        self.assertNotIn('model_providers', data)
        self.assertIn('[model_providers.fake]', data['mcp_servers']['shared']['description'])
        self.assertFalse(data['skills']['config'][0]['enabled'])
        self.assertNotIn('secret_auth_storage', data['features'])
        self.assertTrue(data['features']['js_repl'])

    def test_unclosed_multiline_value_is_rejected(self):
        with self.assertRaises(resources.ResourceError):
            resources.merge_config(self.original, 'notify = ["unfinished"\n')

    def test_inline_quoted_and_dotted_auth_feature_cannot_disable_encryption(self):
        for source in (
            'features = { secret_auth_storage = false, js_repl = true }',
            '"features"."secret_auth_storage" = false\nfeatures.js_repl = true',
            '["features"]\n"secret_auth_storage" = false\njs_repl = true',
        ):
            with self.subTest(source=source):
                merged = resources.merge_config(self.original, source)
                self.assertNotIn('secret_auth_storage', merged)
                self.assertIn('"js_repl" = true', merged)
                self.assertEqual(resources.merge_config(merged, source), merged)

    @unittest.skipIf(sys.version_info < (3, 11), 'TOML semantic validation requires Python 3.11+')
    def test_nested_feature_forms_preserve_values_and_auth_isolation(self):
        import tomllib
        for source in (
            '[features.context_management]\nexperimental_mode = true\n',
            'features.context_management.experimental_mode = true\n',
            '[features]\ncontext_management.experimental_mode = true\n',
            'features = { context_management = { experimental_mode = true }, secret_auth_storage = false }\n',
            '["features"."context_management"]\n"experimental_mode" = true\n',
            '[features.context_management]\n[features.context_management.inner]\nflag = true\n',
        ):
            with self.subTest(source=source):
                merged = resources.merge_config(self.original, source)
                data = tomllib.loads(merged)
                expected = tomllib.loads(source)['features']
                expected.pop('secret_auth_storage', None)
                self.assertEqual(data['features'], expected)
                self.assertEqual(data['cli_auth_credentials_store'], 'keyring')
                self.assertEqual(data['forced_login_method'], 'chatgpt')
                self.assertEqual(resources.merge_config(merged, source), merged)

    @unittest.skipIf(sys.version_info < (3, 11), 'TOML semantic validation requires Python 3.11+')
    def test_nested_feature_values_and_quoted_auth_key(self):
        import tomllib
        source = '''[features]
hooks = true
"secret_auth_storage" = false
"secret_auth_storage.label" = "ordinary quoted key"
[features.context_management]
experimental_mode = true
label = "a,#=b"
limits = [1, 2, 3] # preserved array
options = { mode = "x", enabled = true }
description = """multi
# not a comment
[features.secret_auth_storage]
"""
[features.empty]
'''
        expected = tomllib.loads(source)
        del expected['features']['secret_auth_storage']
        merged = resources.merge_config(self.original, source)
        self.assertEqual(tomllib.loads(merged)['features'], expected['features'])
        self.assertEqual(resources.merge_config(merged, source), merged)

    @unittest.skipIf(sys.version_info < (3, 11), 'TOML semantic validation requires Python 3.11+')
    def test_nested_auth_feature_is_not_shared(self):
        import tomllib
        for source in (
            '[features.secret_auth_storage]\nenabled = false\n',
            'features.secret_auth_storage.enabled = false\n',
            'features = { secret_auth_storage = { enabled = false }, hooks = true }\n',
        ):
            with self.subTest(source=source):
                merged = resources.merge_config(self.original, source)
                self.assertNotIn('secret_auth_storage', tomllib.loads(merged).get('features', {}))

    def test_preview_is_read_only(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        directories = {str(p) for p in self.root.rglob('*') if p.is_dir()}
        report = resources.sync(self.home, self.source, apiagent, dry_run=True)
        self.assertTrue(report['configChanged'])
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        self.assertEqual(directories, {str(p) for p in self.root.rglob('*') if p.is_dir()})

    def test_shared_hook_definitions_preserve_local_trust_and_backup_previous_hooks(self):
        source_hook = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'shared-statusline'}]}]}}
        old_hook = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'previous-local-hook'}]}]}}
        (self.source / 'hooks.json').write_text(json.dumps(source_hook))
        (self.home / 'hooks.json').write_text(json.dumps(old_hook))
        with (self.home / 'config.toml').open('a') as stream:
            stream.write('\n[hooks.state.local-hook]\ntrusted_hash = "local-reviewed-hash"\n')
        with (self.source / 'config.toml').open('a') as stream:
            stream.write('\n[hooks.state.other-hook]\ntrusted_hash = "source-only-hash"\n')
        report = resources.sync(self.home, self.source, apiagent)
        self.assertEqual(json.loads((self.home / 'hooks.json').read_text()), source_hook)
        self.assertEqual(json.loads((Path(report['backup']) / 'hooks.json').read_text()), old_hook)
        merged = (self.home / 'config.toml').read_text()
        self.assertIn('local-reviewed-hash', merged)
        self.assertNotIn('source-only-hash', merged)
        source_hook['hooks']['Stop'][0]['hooks'][0]['command'] = 'changed-statusline'
        (self.source / 'hooks.json').write_text(json.dumps(source_hook))
        resources.sync(self.home, self.source, apiagent)
        self.assertEqual(json.loads((self.home / 'hooks.json').read_text()), source_hook)
        self.assertIn('local-reviewed-hash', (self.home / 'config.toml').read_text())

    def test_skill_write_through_preserves_originals_and_never_links_auth(self):
        (self.home / 'skills' / 'same').mkdir(parents=True)
        (self.home / 'skills' / 'same' / 'SKILL.md').write_text('local variant')
        (self.home / 'skills' / 'unique').mkdir()
        (self.home / 'skills' / 'unique' / 'SKILL.md').write_text('local only')
        (self.source / 'skills' / 'same').mkdir(parents=True)
        (self.source / 'skills' / 'same' / 'SKILL.md').write_text('official shared variant')
        (self.source / 'AGENTS.md').write_text('shared rules')
        (self.home / 'secrets').mkdir()
        credential = self.home / 'secrets' / 'codex_auth.age'
        credential.write_bytes(b'synthetic encrypted auth')
        (self.source / 'secrets').mkdir()
        (self.source / 'secrets' / 'codex_auth.age').write_bytes(b'default encrypted auth sentinel')
        report = resources.sync(self.home, self.source, apiagent)
        self.assertTrue((self.home / 'skills').samefile(self.source / 'skills'))
        self.assertEqual((self.home / 'skills' / 'same' / 'SKILL.md').read_text(), 'official shared variant')
        backup = Path(report['backup'])
        self.assertEqual((backup / 'skills' / 'same' / 'SKILL.md').read_text(), 'local variant')
        self.assertEqual((self.source / 'skills' / 'unique' / 'SKILL.md').read_text(), 'local only')
        created = self.home / 'skills' / 'added-through-profile'
        created.mkdir()
        (created / 'SKILL.md').write_text('new shared skill')
        self.assertEqual((self.source / 'skills' / created.name / 'SKILL.md').read_text(), 'new shared skill')
        self.assertEqual(credential.read_bytes(), b'synthetic encrypted auth')
        self.assertFalse((self.home / 'secrets').samefile(self.source / 'secrets'))
        self.assertFalse((self.home / 'config.toml').samefile(self.source / 'config.toml'))
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'shared rules')
        again = resources.sync(self.home, self.source, apiagent)
        self.assertIsNone(again['backup'])
        self.assertFalse(again['configChanged'])
        (self.source / 'config.toml').write_text('[mcp_servers.updated]\ncommand = "updated-tool"\n')
        (self.source / 'AGENTS.md').write_text('updated shared rules')
        resources.sync(self.home, self.source, apiagent)
        self.assertIn('[mcp_servers.updated]', (self.home / 'config.toml').read_text())
        self.assertNotIn('[mcp_servers.shared]', (self.home / 'config.toml').read_text())
        self.assertEqual((self.home / 'AGENTS.md').read_text(), 'updated shared rules')
        (self.source / 'AGENTS.md').unlink()
        removed = resources.sync(self.home, self.source, apiagent)
        self.assertFalse((self.home / 'AGENTS.md').exists())
        self.assertEqual((Path(removed['backup']) / 'AGENTS.md').read_text(), 'updated shared rules')

    def test_failed_link_restores_local_skill_directory(self):
        (self.home / 'skills').mkdir()
        (self.home / 'skills' / 'sentinel').write_text('preserve')
        with patch.object(resources, 'link_directory', side_effect=OSError('synthetic failure')):
            with self.assertRaises(OSError):
                resources.sync(self.home, self.source, apiagent)
        self.assertEqual((self.home / 'skills' / 'sentinel').read_text(), 'preserve')
        self.assertEqual((self.home / 'config.toml').read_text(), self.original)

    def test_live_plugin_cache_defers_only_its_link_and_keeps_other_sharing(self):
        cache = self.home / 'plugins' / 'cache'
        cache.mkdir(parents=True)
        (cache / 'loaded-plugin').write_text('retained')
        original_rename = Path.rename
        def busy(path, destination):
            if path == cache:
                raise PermissionError('synthetic open Desktop package')
            return original_rename(path, destination)
        with patch.object(Path, 'rename', busy):
            report = resources.sync(self.home, self.source, apiagent)
        self.assertEqual(report['deferredDirectories'], ['plugins/cache'])
        self.assertTrue((self.home / 'skills').samefile(self.source / 'skills'))
        self.assertEqual((cache / 'loaded-plugin').read_text(), 'retained')
        self.assertIn('[mcp_servers.shared]', (self.home / 'config.toml').read_text())
        retry = resources.sync(self.home, self.source, apiagent)
        self.assertEqual(retry['deferredDirectories'], [])
        self.assertTrue(cache.samefile(self.source / 'plugins/cache'))


if __name__ == '__main__':
    unittest.main()
