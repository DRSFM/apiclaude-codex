from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import apiagent
import codex_accounts as accounts
import codex_account_menu as menu
from tests.test_codex_oauth import synthetic_auth


class AccountMenuTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in {'HOME': self.root, 'CODEX_HOME': self.root / '.codex-api',
                            'CODEX_PROFILES_PATH': self.root / '.codex-api' / 'profiles.json',
                            'CODEX_DESKTOP_DATA_ROOT': self.root / '.desktop'}.items():
            p = patch.object(apiagent, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.api = {'id': 'api', 'name': 'API node', 'type': 'api_key', 'home': 'profiles/api'}
        self.account = {'id': 'chatgpt-test', 'name': 'user@example.invalid', 'type': 'chatgpt', 'home': 'accounts/chatgpt-test'}
        apiagent.save_codex_profiles([self.api, self.account])

    def test_zero_is_always_available_even_without_api_profiles(self):
        for profiles in ([], [self.api], [self.account], [self.api, self.account]):
            output = io.StringIO()
            with patch('builtins.input', side_effect=['0', '1']), redirect_stdout(output):
                selected = menu.choose(profiles, apiagent)
            self.assertEqual(selected['type'], 'official_default')
            self.assertIn('[0] Account login', output.getvalue())
            self.assertIn('[1] Official login', output.getvalue())

    def test_top_level_api_numbers_exclude_subscription_accounts(self):
        with patch('builtins.input', return_value='1'), redirect_stdout(io.StringIO()):
            self.assertEqual(menu.choose([self.account, self.api], apiagent), self.api)

    def test_account_submenu_and_back_quit(self):
        with patch('builtins.input', side_effect=['0', '2']), redirect_stdout(io.StringIO()):
            self.assertEqual(menu.choose([self.api, self.account], apiagent), self.account)
        with patch('builtins.input', side_effect=['0', 'b', 'q']), redirect_stdout(io.StringIO()):
            self.assertEqual(menu.choose([self.api, self.account], apiagent), menu.CANCEL)

    def test_default_cli_uses_default_home_and_original_arguments_without_login(self):
        with patch('builtins.input', side_effect=['0', '1']), redirect_stdout(io.StringIO()), \
             patch.object(apiagent, 'find_official_codex_cli_executable', return_value='official-codex'), \
             patch.object(apiagent, 'run_command', return_value=0) as run, \
             patch.object(accounts, 'sync_resources', side_effect=AssertionError('default must not be synchronized')), \
             patch.object(apiagent, 'get_codex_secret', side_effect=AssertionError('API key read')):
            self.assertEqual(apiagent.codex_main(['--resume']), 0)
        self.assertEqual(run.call_args.args, ('official-codex', ['--resume']))
        self.assertEqual(run.call_args.kwargs['env']['CODEX_HOME'], str(self.root / '.codex'))
        self.assertIn('OPENAI_API_KEY', run.call_args.kwargs['env_remove'])
        self.assertFalse((self.root / '.codex').exists())

    def test_default_desktop_has_no_profile_user_data_override(self):
        with patch('builtins.input', side_effect=['0', '1']), redirect_stdout(io.StringIO()), \
             patch.object(apiagent, 'find_codex_desktop_executable', return_value=Path('ChatGPT.exe')), \
             patch.object(apiagent, 'start_detached_process', return_value=0) as start:
            self.assertEqual(apiagent.codex_main(['--desktop']), 0)
        self.assertEqual(start.call_args.args[1], [])
        self.assertEqual(start.call_args.kwargs['env']['CODEX_HOME'], str(self.root / '.codex'))

    def test_rename_retains_old_alias_home_and_credentials(self):
        home = accounts.ensure_config(self.account, apiagent)
        (home / 'secrets').mkdir()
        auth = home / 'secrets' / 'codex_auth.age'
        auth.write_bytes(b'synthetic ciphertext')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.main(['rename', self.account['name'], 'short-name'], apiagent), 0)
        selected = apiagent.find_profile(accounts.registry_profiles(apiagent), self.account['name'])
        self.assertEqual(selected['name'], 'short-name')
        self.assertEqual(selected['home'], self.account['home'])
        self.assertEqual(selected['id'], self.account['id'])
        self.assertEqual(auth.read_bytes(), b'synthetic ciphertext')

    def test_direct_import_uses_email_when_name_is_omitted(self):
        path = self.root / 'explicit.json'
        path.write_text(json.dumps(synthetic_auth('direct-import')))
        before = apiagent.CODEX_PROFILES_PATH.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            code = accounts.main(['import', '--file', str(path), '--dry-run'], apiagent)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['name'], 'direct-import@example.invalid')
        self.assertEqual(apiagent.CODEX_PROFILES_PATH.read_bytes(), before)
        self.assertFalse((self.root / '.codex-api' / 'accounts').exists())

    def test_oauth_add_suggests_email_and_passes_only_file_path_to_import(self):
        path = self.root / 'oauth.json'
        path.write_text(json.dumps(synthetic_auth('new-account')))
        calls = []
        def do_import(args, api):
            calls.append(args)
            created = dict(self.account, id='chatgpt-new', name=args[1], home='accounts/chatgpt-new')
            api.save_codex_profiles([self.api, self.account, created])
            return 0
        with patch('builtins.input', side_effect=['2', str(path), '']) as prompt, \
             patch.object(accounts, 'main', side_effect=do_import), redirect_stdout(io.StringIO()):
            result = menu.add_account(apiagent)
        self.assertEqual(result['name'], 'new-account@example.invalid')
        self.assertIn('[new-account@example.invalid]', prompt.call_args.args[0])
        self.assertEqual(calls[0], ['import', 'new-account@example.invalid', '--file', str(path)])

    def test_browser_add_uses_isolated_home_then_official_email(self):
        original_main = accounts.main
        logins = []
        def dispatch(args, api):
            if args[0] == 'login':
                selected = accounts.selected_profile(accounts.registry_profiles(api), args[1], api)
                logins.append(accounts.profile_home(selected, api))
                return 0
            return original_main(args, api)
        with patch('builtins.input', side_effect=['1', '']), redirect_stdout(io.StringIO()), \
             patch.object(accounts, 'main', side_effect=dispatch), \
             patch.object(apiagent, 'find_codex_cli_executable', return_value='official'), \
             patch.object(accounts, 'read_account', return_value={'type': 'chatgpt', 'email': 'browser@example.invalid'}):
            created = menu.add_account(apiagent)
        self.assertEqual(created['name'], 'browser@example.invalid')
        self.assertNotEqual(logins[0], self.root / '.codex')
        self.assertEqual(logins[0], accounts.profile_home(created, apiagent))

    def test_mcp_add_routes_to_shared_default_and_then_synchronizes(self):
        with patch.object(accounts, 'launch_default', return_value=0) as default, \
             patch.object(accounts, 'sync_resources') as sync, redirect_stdout(io.StringIO()):
            self.assertEqual(accounts.launch(self.account, ['mcp', 'add', 'sample', '--url', 'https://example.invalid/mcp'], apiagent), 0)
        self.assertEqual(default.call_args.args[0][:3], ['mcp', 'add', 'sample'])
        sync.assert_called_once()


if __name__ == '__main__':
    unittest.main()
