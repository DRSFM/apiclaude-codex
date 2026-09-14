from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent


class CodexModelRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.api_root = self.root / '.codex-api'
        self.home = self.api_root / 'profiles' / 'relay'
        self.home.mkdir(parents=True)
        self.profile = {
            'id': 'relay', 'name': 'relay', 'home': 'profiles/relay',
            'baseUrl': 'https://example.test/v1', 'credentialId': 'custom-reference',
            'model': 'gpt-5.6-sol', 'useCustomCodexCli': False,
        }
        self.catalog_path = self.home / 'models.json'
        self.config_path = self.home / 'config.toml'
        self.state_path = self.home / 'models-refresh.json'
        self.original = apiagent.build_codex_provider_catalog(['gpt-5.6-sol', 'vendor-old'], 'gpt-5.6-sol')
        self.original['models'][0]['context_window'] = 272000
        self.original['models'][1]['visibility'] = 'hide'
        self.catalog_path.write_text(json.dumps(self.original), encoding='utf-8')
        self.config_path.write_text(
            'model = "gpt-5.6-sol"\n# preserve this comment\n'
            f'model_catalog_json = "{self.catalog_path.as_posix()}"\n'
            '[mcp_servers.test]\ncommand = "test"\n', encoding='utf-8',
        )
        self.profiles_path = self.api_root / 'profiles.json'
        self.profiles_path.write_text(json.dumps({'profiles': [self.profile]}), encoding='utf-8')
        self.builtin = apiagent.build_codex_provider_catalog(['gpt-6-astra'], 'gpt-6-astra')
        self.builtin['models'][0].update({
            'context_window': 1000000, 'input_modalities': ['text', 'image'],
            'supported_reasoning_levels': [{'effort': 'ultra'}],
        })
        patches = {
            'HOME': self.root, 'CODEX_HOME': self.api_root,
            'CODEX_PROFILES_PATH': self.profiles_path,
        }
        for name, value in patches.items():
            p = patch.object(apiagent, name, value)
            p.start()
            self.addCleanup(p.stop)
        for name, value in (
            ('get_codex_secret', 'sk-private-test'),
            ('fetch_codex_provider_models', ['gpt-6-astra', 'gpt-image-2']),
            ('fetch_codex_builtin_model_catalog', self.builtin),
        ):
            p = patch.object(apiagent, name, return_value=value)
            setattr(self, name, p.start())
            self.addCleanup(p.stop)
        env = patch.dict(os.environ, {'APICODEX_AUTO_REFRESH_MODELS': ''})
        env.start()
        self.addCleanup(env.stop)

    def test_refresh_adds_astra_and_keeps_defaults_metadata_and_backup(self) -> None:
        config = self.config_path.read_bytes()
        catalog = self.catalog_path.read_bytes()
        metadata = self.profiles_path.read_bytes()
        result = apiagent.refresh_codex_models(self.profile)
        self.assertEqual(result, {'status': 'updated', 'added': 1})
        payload = json.loads(self.catalog_path.read_text(encoding='utf-8'))
        self.assertEqual(payload['models'][:2], self.original['models'])
        astra = payload['models'][-1]
        self.assertEqual(astra['slug'], 'gpt-6-astra')
        self.assertEqual(astra['context_window'], 1000000)
        self.assertEqual(astra['supported_reasoning_levels'], [{'effort': 'ultra'}])
        self.assertEqual(astra['priority'], 3)
        self.assertEqual(self.config_path.read_bytes(), config)
        self.assertEqual(self.profiles_path.read_bytes(), metadata)
        backups = list(self.home.glob('models.json.backup-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), catalog)
        self.fetch_codex_provider_models.assert_called_once_with('https://example.test/v1', 'sk-private-test', timeout=5)
        self.get_codex_secret.assert_called_once_with(self.profile)
        for path in self.home.iterdir():
            self.assertNotIn(b'sk-private-test', path.read_bytes())

    def test_cache_skips_network_and_forced_refresh_is_idempotent(self) -> None:
        apiagent.refresh_codex_models(self.profile)
        before = self.catalog_path.stat().st_mtime_ns
        self.fetch_codex_provider_models.reset_mock()
        self.get_codex_secret.reset_mock()
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'skipped')
        self.fetch_codex_provider_models.assert_not_called()
        self.get_codex_secret.assert_not_called()
        self.assertEqual(apiagent.refresh_codex_models(self.profile, force=True)['status'], 'unchanged')
        self.assertEqual(self.catalog_path.stat().st_mtime_ns, before)
        self.assertEqual(len(list(self.home.glob('models.json.backup-*'))), 1)

    def test_expiry_catalog_edit_and_source_change_invalidate_cache(self) -> None:
        apiagent.refresh_codex_models(self.profile)
        for change in ('expiry', 'catalog', 'source', 'corrupt-state'):
            with self.subTest(change=change):
                if change == 'expiry':
                    state = json.loads(self.state_path.read_text())
                    state['checkedAt'] = 1
                    self.state_path.write_text(json.dumps(state))
                elif change == 'catalog':
                    self.catalog_path.write_bytes(self.catalog_path.read_bytes() + b' ')
                elif change == 'source':
                    self.profile['baseUrl'] = 'https://other.test/v1'
                else:
                    self.state_path.write_text('[]')
                self.fetch_codex_provider_models.reset_mock()
                self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'unchanged')
                self.fetch_codex_provider_models.assert_called_once()

    def test_failures_keep_catalog_and_never_leak_secret(self) -> None:
        original = self.catalog_path.read_bytes()
        for error in (ValueError('model discovery returned HTTP 503 sk-private-test'),
                      OSError('sk-private-test'), KeyError('sk-private-test')):
            with self.subTest(error=type(error).__name__):
                self.fetch_codex_provider_models.side_effect = error
                output = io.StringIO()
                with redirect_stderr(output):
                    apiagent.auto_refresh_codex_models(self.profile)
                self.assertIn('continuing with the existing catalog', output.getvalue())
                self.assertNotIn('sk-private-test', output.getvalue())
                self.assertEqual(self.catalog_path.read_bytes(), original)
                self.assertFalse(self.state_path.exists())
                self.assertEqual(list(self.home.glob('models.json.backup-*')), [])

    def test_dry_run_reads_saved_key_without_writing_profile_files(self) -> None:
        before = {p.name: p.read_bytes() for p in self.home.iterdir()}
        with patch.object(apiagent, 'load_codex_profiles', side_effect=AssertionError('migration')), redirect_stdout(io.StringIO()):
            code = apiagent.codex_main(['models', 'refresh', '--api-profile', 'relay', '--dry-run'])
        self.assertEqual(code, 0)
        self.get_codex_secret.assert_called_once_with(self.profile)
        self.assertEqual({p.name: p.read_bytes() for p in self.home.iterdir()}, before)

    def test_invalid_upstream_json_has_actionable_safe_error(self) -> None:
        self.fetch_codex_provider_models.side_effect = ValueError('model discovery returned invalid JSON')
        result = apiagent.refresh_codex_models(self.profile)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['reason'], 'model discovery returned invalid JSON')

    def test_external_builtin_and_disabled_catalogs_do_not_fetch(self) -> None:
        for config in ('model = "gpt-5.6-sol"\n', 'model_catalog_json = "other.json"\n'):
            self.config_path.write_text(config)
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'skipped')
        with patch.dict(os.environ, {'APICODEX_AUTO_REFRESH_MODELS': '0'}):
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'skipped')
        self.fetch_codex_provider_models.assert_not_called()

    def test_corrupt_catalog_unsafe_home_and_empty_discovery_are_preserved(self) -> None:
        for payload in ('{}', '{', '{"models": [null]}'):
            self.catalog_path.write_text(payload)
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
            self.assertEqual(self.catalog_path.read_text(), payload)
        self.fetch_codex_provider_models.assert_not_called()
        self.profile['home'] = '../.codex'
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.profile['home'] = 'profiles/relay'
        self.catalog_path.write_text(json.dumps(self.original))
        self.fetch_codex_provider_models.return_value = ['gpt-image-2']
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.assertFalse(self.state_path.exists())

    def test_concurrent_catalog_edit_is_not_overwritten(self) -> None:
        manual = json.dumps({'models': self.original['models'], 'manual': True})
        def discover(*args, **kwargs):
            self.catalog_path.write_text(manual)
            return ['gpt-6-astra']
        self.fetch_codex_provider_models.side_effect = discover
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.assertEqual(self.catalog_path.read_text(), manual)
        self.assertFalse(self.state_path.exists())

    def test_atomic_replace_failure_preserves_original(self) -> None:
        original = self.catalog_path.read_bytes()
        with patch.object(apiagent, 'install_codex_model_catalog', side_effect=OSError('disk full')):
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.assertEqual(self.catalog_path.read_bytes(), original)
        self.assertFalse(self.state_path.exists())

    def test_vision_profile_applies_image_support_to_new_models(self) -> None:
        self.profile['vision'] = {'enabled': True, 'proxyPort': 12345}
        self.builtin['models'][0]['input_modalities'] = ['text']
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'updated')
        models = json.loads(self.catalog_path.read_text())['models']
        self.assertEqual(models[-1]['input_modalities'], ['text', 'image'])

    def test_astra_fallback_has_reasoning_levels(self) -> None:
        self.fetch_codex_builtin_model_catalog.return_value = None
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'updated')
        model = json.loads(self.catalog_path.read_text())['models'][-1]
        self.assertEqual([r['effort'] for r in model['supported_reasoning_levels']], ['low', 'medium', 'high', 'xhigh'])

    def test_cli_parser_rejects_ambiguous_options_without_reading_keys(self) -> None:
        for args in (['refresh', '--all', '--api-profile', 'relay'], ['refresh', '--api-profile'], ['other'], ['refresh', '--account']):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                self.assertEqual(apiagent.codex_models_main(args), 1)
        self.get_codex_secret.assert_not_called()

    def test_all_reports_failure_but_refreshes_other_profiles(self) -> None:
        self.profiles_path.write_text(json.dumps({'profiles': [self.profile, {**self.profile, 'id': 'bad', 'home': '../.codex'}]}))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(apiagent.codex_models_main(['refresh', '--all']), 1)
        self.assertEqual(json.loads(self.catalog_path.read_text())['models'][-1]['slug'], 'gpt-6-astra')

    def test_refresh_lock_rejects_overlap_and_releases_after_failure(self) -> None:
        with apiagent.codex_model_refresh_lock(self.home):
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
            self.fetch_codex_provider_models.assert_not_called()
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'updated')

    def test_backup_failure_prevents_catalog_replace(self) -> None:
        original = self.catalog_path.read_bytes()
        real_open = Path.open
        def fail_backup(path, *args, **kwargs):
            if '.backup-' in path.name:
                raise PermissionError('backup unavailable')
            return real_open(path, *args, **kwargs)
        with patch.object(Path, 'open', fail_backup):
            self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.assertEqual(self.catalog_path.read_bytes(), original)
        self.assertFalse(self.state_path.exists())

    def test_forced_refresh_overrides_disabled_auto_refresh(self) -> None:
        with patch.dict(os.environ, {'APICODEX_AUTO_REFRESH_MODELS': '0'}):
            self.assertEqual(apiagent.refresh_codex_models(self.profile, force=True)['status'], 'updated')

    def test_catalog_symlink_cannot_write_external_file(self) -> None:
        external = self.root / 'external.json'
        original = self.catalog_path.read_bytes()
        external.write_bytes(original)
        self.catalog_path.unlink()
        try:
            self.catalog_path.symlink_to(external)
        except OSError:
            self.skipTest('symlinks unavailable')
        self.assertEqual(apiagent.refresh_codex_models(self.profile)['status'], 'failed')
        self.assertEqual(external.read_bytes(), original)
        self.fetch_codex_provider_models.assert_not_called()

    def test_launches_refresh_before_child_starts_and_version_stays_offline(self) -> None:
        modes = [[], ['--vscode'], ['--version'], ['--help']]
        if os.name == 'nt':
            modes.append(['--desktop'])
        for mode in modes:
            with self.subTest(mode=mode), ExitStack() as stack:
                self.catalog_path.write_text(json.dumps(self.original))
                self.state_path.unlink(missing_ok=True)
                self.fetch_codex_provider_models.reset_mock()
                offline = mode in (['--version'], ['--help'])
                def start(*args, **kwargs):
                    slugs = [m['slug'] for m in json.loads(self.catalog_path.read_text())['models']]
                    self.assertEqual('gpt-6-astra' in slugs, not offline)
                    return 0
                for name, value in (
                    ('load_codex_profiles', [self.profile]),
                    ('prepare_codex_vision_runtime', True),
                    ('sync_codex_shared_mcp', None),
                    ('repair_codex_home_images', None),
                    ('ensure_codex_keyring_auth', True),
                    ('update_codex_last_used', None),
                    ('add_current_project_trust', None),
                    ('find_codex_cli_executable', 'codex-test'),
                    ('find_codex_desktop_executable', self.root / 'ChatGPT.exe'),
                    ('label_codex_desktop_window', True),
                ):
                    stack.enter_context(patch.object(apiagent, name, return_value=value))
                stack.enter_context(patch.object(apiagent, 'CODEX_DESKTOP_DATA_ROOT', self.root / 'desktop'))
                stack.enter_context(patch.object(apiagent, 'CODEX_VSCODE_DATA_ROOT', self.root / 'vscode'))
                stack.enter_context(patch.object(apiagent, 'run_command', side_effect=start))
                stack.enter_context(patch.object(apiagent, 'start_detached_process', side_effect=start))
                stack.enter_context(patch.dict(os.environ, {'APICODEX_DREAM_SKIN_SCRIPT': ''}))
                stack.enter_context(redirect_stdout(io.StringIO()))
                stack.enter_context(redirect_stderr(io.StringIO()))
                self.assertEqual(apiagent.codex_main(['--api-profile', 'relay', *mode]), 0)
                if offline:
                    self.fetch_codex_provider_models.assert_not_called()
                else:
                    self.fetch_codex_provider_models.assert_called_once()


if __name__ == '__main__':
    unittest.main()
