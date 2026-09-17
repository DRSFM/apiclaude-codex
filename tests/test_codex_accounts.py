"""Subscription profiles must never use the API login path or default account."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import apiagent
import codex_accounts as accounts


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for key, value in {
            'HOME': self.root,
            'CODEX_HOME': self.root / '.codex-api',
            'CODEX_PROFILES_PATH': self.root / '.codex-api' / 'profiles.json',
            'CODEX_ARCHIVE_ROOT': self.root / '.codex-api' / 'archived-profiles',
            'CODEX_DESKTOP_DATA_ROOT': self.root / '.apicodex-desktop',
        }.items():
            p = patch.object(apiagent, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(apiagent, 'sync_codex_shared_mcp')
        p.start()
        self.addCleanup(p.stop)

    def create(self, name='work', model=None):
        args = ['add', name] + (['--model', model] if model else [])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.main(args, apiagent), 0)
        return apiagent.find_profile(apiagent.load_codex_profiles(), name)

    def test_create_independent_profiles_without_login(self):
        with patch.object(apiagent, 'run_command') as run:
            a, b = self.create('a'), self.create('b', 'gpt-6-astra')
        self.assertNotEqual(a['id'], b['id'])
        self.assertNotEqual(a['home'], b['home'])
        self.assertEqual(a['type'], 'chatgpt')
        run.assert_not_called()
        self.assertFalse((self.root / '.codex').exists())
        config = (apiagent.codex_profile_home(b) / 'config.toml').read_text()
        self.assertIn('model_provider = "openai"', config)
        self.assertNotIn('model_catalog_json', config)
        self.assertNotIn('APICODEX_API_KEY', config)

    def test_cli_reuses_credentials_without_login_or_api_setup(self):
        profile = self.create()
        with (
            patch.object(apiagent, 'find_codex_cli_executable', return_value='codex.exe'),
            patch.object(apiagent, 'run_command', return_value=0) as run,
            patch.object(apiagent, 'get_codex_secret', side_effect=AssertionError('API secret read')),
            patch.object(apiagent, 'auto_refresh_codex_models', side_effect=AssertionError('API refresh')),
            patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-parent', 'CODEX_ACCESS_TOKEN': 'synthetic-parent', 'CODEX_THREAD_ID': 'parent', 'APICODEX_API_KEY': 'synthetic-parent'}),
        ):
            self.assertEqual(apiagent.codex_main(['--account-profile', 'work', '--model', 'gpt-6-astra']), 0)
        args = run.call_args.args[1]
        self.assertNotIn('login', args)
        self.assertNotIn('apps', args)
        self.assertNotIn(apiagent.CODEX_EPHEMERAL_AUTH_OVERRIDE, args)
        self.assertIn('gpt-6-astra', args)
        self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'], str(apiagent.codex_profile_home(profile)))
        self.assertTrue({'OPENAI_API_KEY', 'APICODEX_API_KEY', 'CODEX_ACCESS_TOKEN', 'CODEX_THREAD_ID'} <= set(run.call_args.kwargs['env_remove']))

    def test_desktop_does_not_clear_or_relogin_auth(self):
        profile = self.create()
        home = apiagent.codex_profile_home(profile)
        (home / 'secrets').mkdir()
        (home / 'secrets' / 'codex_auth.age').write_bytes(b'encrypted-placeholder')
        with (
            patch.object(apiagent, 'find_codex_desktop_executable', return_value=Path('ChatGPT.exe')),
            patch.object(apiagent, 'start_detached_process', return_value=0) as start,
            patch.object(apiagent, 'label_codex_desktop_window', return_value=True),
            patch.object(apiagent, 'ensure_codex_keyring_auth', side_effect=AssertionError('API login')),
        ):
            self.assertEqual(apiagent.codex_main(['--desktop', '--account-profile', 'work']), 0)
        self.assertEqual((home / 'secrets' / 'codex_auth.age').read_bytes(), b'encrypted-placeholder')
        self.assertIn('--user-data-dir=', start.call_args.args[1][0])
        self.assertEqual(start.call_args.kwargs['env']['CODEX_HOME'], str(home))

    def test_account_selector_rejects_api_profile(self):
        apiagent.save_codex_profiles([{'id': 'api', 'name': 'api', 'home': 'profiles/api'}])
        self.assertEqual(apiagent.codex_main(['--account-profile', 'api']), 1)

    def test_secret_migration_skips_chatgpt_even_with_unexpected_api_fields(self):
        profile = self.create()
        profile['apiKey'] = 'synthetic-legacy-field'
        with patch.object(apiagent.SECRET_STORE, 'set', side_effect=AssertionError('migration')):
            self.assertFalse(apiagent.migrate_codex_secrets([profile], apiagent.SECRET_STORE))

    def test_login_uses_valid_cached_account_without_browser(self):
        profile = self.create()
        with patch.object(accounts, 'read_account', return_value={'type': 'chatgpt', 'email': 'test@example.test'}), patch.object(apiagent, 'run_command') as run:
            self.assertEqual(accounts.main(['login', profile['name']], apiagent), 0)
        run.assert_not_called()

    def test_permanent_login_failure_allows_official_reauth_but_network_failure_does_not(self):
        self.create()
        with patch.object(apiagent, 'find_codex_cli_executable', return_value='codex.exe'), patch.object(accounts, 'profile_busy', return_value=False), patch.object(apiagent, 'run_command', return_value=0) as run:
            with patch.object(accounts, 'read_account', side_effect=accounts.LoginRequired('expired')):
                self.assertEqual(accounts.main(['login', 'work', '--device-auth'], apiagent), 0)
            self.assertEqual(run.call_args.args[1], ['login', '--device-auth'])
            run.reset_mock()
            with patch.object(accounts, 'read_account', side_effect=accounts.AccountError('network failure')):
                self.assertEqual(accounts.main(['login', 'work'], apiagent), 1)
            run.assert_not_called()

    def test_dry_run_leaves_account_metadata_and_files_unchanged(self):
        self.create()
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        for command in ('archive', 'logout'):
            self.assertEqual(accounts.main([command, 'work', '--dry-run'], apiagent), 0)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_config_auth_override_fails_before_launch(self):
        self.create()
        with patch.object(apiagent, 'run_command') as run:
            self.assertEqual(apiagent.codex_main(['--account-profile', 'work', '-c', 'cli_auth_credentials_store="file"']), 1)
        run.assert_not_called()

    def test_archive_preserves_files_and_does_not_remove_other_profile(self):
        a, b = self.create('a'), self.create('b')
        home = apiagent.codex_profile_home(a)
        (home / 'history.jsonl').write_text('preserved-history')
        with patch.object(accounts, 'profile_busy', return_value=False):
            self.assertEqual(accounts.main(['archive', 'a', '--yes'], apiagent), 0)
        self.assertFalse(home.exists())
        self.assertTrue(apiagent.codex_profile_home(b).exists())
        self.assertEqual([p['name'] for p in apiagent.load_codex_profiles()], ['b'])
        archives = list(apiagent.CODEX_ARCHIVE_ROOT.iterdir())
        self.assertEqual((archives[0] / 'home' / 'history.jsonl').read_text(), 'preserved-history')
        manifest = json.loads((archives[0] / 'profile.json').read_text())
        self.assertEqual(manifest['originalHome'], str(home))

    def test_failed_archive_registry_save_restores_source(self):
        p = self.create()
        home = apiagent.codex_profile_home(p)
        with patch.object(accounts, 'profile_busy', return_value=False), patch.object(apiagent, 'save_codex_profiles', side_effect=OSError('disk full')):
            self.assertEqual(accounts.main(['archive', 'work', '--yes'], apiagent), 1)
        self.assertTrue(home.is_dir())
        self.assertIsNotNone(apiagent.find_profile(apiagent.load_codex_profiles(), 'work'))

    def test_active_profile_cannot_be_archived_or_logged_out(self):
        self.create()
        with patch.object(accounts, 'profile_busy', return_value=True), patch.object(apiagent, 'run_command') as run:
            self.assertEqual(accounts.main(['archive', 'work', '--yes'], apiagent), 1)
            self.assertEqual(accounts.main(['logout', 'work', '--yes'], apiagent), 1)
        run.assert_not_called()

    def test_logout_only_uses_selected_home(self):
        a, b = self.create('a'), self.create('b')
        with patch.object(accounts, 'profile_busy', return_value=False), patch.object(apiagent, 'find_codex_cli_executable', return_value='codex.exe'), patch.object(apiagent, 'run_command', return_value=0) as run:
            self.assertEqual(accounts.main(['logout', 'b', '--yes'], apiagent), 0)
        self.assertEqual(run.call_args.args[1], ['logout'])
        self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'], str(apiagent.codex_profile_home(b)))
        self.assertTrue(apiagent.codex_profile_home(a).exists())

    def test_model_changes_preserve_preferences_and_credentials(self):
        p = self.create(model='gpt-6-astra')
        home = apiagent.codex_profile_home(p)
        config = home / 'config.toml'
        with config.open('a') as f:
            f.write('\n[mcp_servers.local]\ncommand = "my-tool"\n')
        self.assertEqual(accounts.main(['model', 'work', 'gpt-5.6-sol'], apiagent), 0)
        self.assertIn('model = "gpt-5.6-sol"', config.read_text())
        self.assertIn('command = "my-tool"', config.read_text())

    def test_api_update_cannot_overwrite_named_account(self):
        self.create()
        with patch.object(apiagent.SECRET_STORE, 'set', side_effect=AssertionError('credential changed')):
            self.assertEqual(apiagent.add_codex_profile(name='work'), 1)

    def test_api_model_refresh_skips_account_without_reading_key(self):
        p = self.create()
        with patch.object(apiagent, 'get_codex_secret', side_effect=AssertionError('API secret')):
            self.assertEqual(apiagent.refresh_codex_models(p, force=True)['status'], 'skipped')

    def test_dry_run_does_not_recreate_missing_config(self):
        profile = self.create()
        path = apiagent.codex_profile_home(profile) / 'config.toml'
        path.unlink()
        for command in ('logout', 'archive'):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(accounts.main([command, 'work', '--dry-run'], apiagent), 0)
        self.assertFalse(path.exists())

    def test_account_cannot_opt_into_custom_cli(self):
        self.create()
        with patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
            self.assertEqual(apiagent.configure_codex_custom_cli('work'), 1)


if __name__ == '__main__':
    unittest.main()
