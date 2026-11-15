from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

import apiagent as api
import codex_api_keys as keys
from secure_store import SecureStore, SecureStoreError
from tests.support import KeychainIsolationMixin


class ApiKeysTests(KeychainIsolationMixin):
    def setUp(self):
        super().setUp()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / '.codex-api'
        self.home.mkdir()
        self.store = SecureStore(self.root / 'secrets')
        self.store.set('codex:anyrouter', 'synthetic-first-key')
        self.profile = {'id': 'anyrouter', 'name': 'anyrouter', 'home': '.',
                        'baseUrl': 'https://example.test/v1', 'credentialId': 'codex:anyrouter',
                        'model': 'test-model', 'useCustomCodexCli': False}
        for name, value in (
            ('HOME', self.root), ('CODEX_HOME', self.home),
            ('CODEX_PROFILES_PATH', self.home / 'profiles.json'),
            ('CODEX_DESKTOP_DATA_ROOT', self.root / 'desktop'),
            ('CODEX_ARCHIVE_ROOT', self.root / 'archives'), ('SECRET_STORE', self.store),
        ):
            p = patch.object(api, name, value)
            p.start()
            self.addCleanup(p.stop)
        api.save_codex_profiles([self.profile])
        (self.home / 'config.toml').write_text('model = "test-model"\n# retain custom settings\n', encoding='utf-8')
        (self.home / 'sessions').mkdir()
        (self.home / 'sessions' / 'chat.jsonl').write_bytes(b'existing conversation')
        desktop = api.CODEX_DESKTOP_DATA_ROOT / 'anyrouter'
        desktop.mkdir(parents=True)
        (desktop / 'sentinel').write_bytes(b'existing desktop data')

    def current(self):
        return keys.registry_profiles(api)[0]

    def add(self):
        with patch.object(api, 'getpass', return_value='\ufeffsynthetic-second-key'), redirect_stdout(io.StringIO()):
            self.assertEqual(api.codex_main(['keys', 'add', 'anyrouter', 'account-2']), 0)
        return self.current()

    def test_add_preserves_existing_primary_key_history_config_and_desktop(self):
        before = {p: p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        profile = self.add()
        self.assertEqual(profile['credentialId'], 'codex:anyrouter')
        self.assertEqual([i['name'] for i in profile['apiKeys']], ['account-1', 'account-2'])
        self.assertEqual(self.store.get('codex:anyrouter'), 'synthetic-first-key')
        self.assertEqual(self.store.get(profile['apiKeys'][1]['credentialId']), 'synthetic-second-key')
        for path, content in before.items():
            if path != api.CODEX_PROFILES_PATH:
                self.assertEqual(path.read_bytes(), content)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'synthetic-first-key', path.read_bytes())
                self.assertNotIn(b'synthetic-second-key', path.read_bytes())

    def test_interactive_selection_persists_and_can_switch_back(self):
        self.add()
        for choice, secret in [('2', 'synthetic-second-key'), ('account-1', 'synthetic-first-key')]:
            profiles = keys.registry_profiles(api)
            with patch.object(keys.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value=choice), redirect_stdout(io.StringIO()):
                self.assertTrue(keys.choose_for_launch(profiles[0], profiles, api))
            self.assertEqual(api.get_codex_secret(profiles[0]), secret)
            self.assertEqual(api.get_codex_secret(self.current()), secret)
            self.assertEqual(profiles[0]['home'], '.')

    def test_default_remembers_last_choice(self):
        self.add()
        keys.use('anyrouter', 'account-2', api)
        profiles = keys.registry_profiles(api)
        with patch.object(keys.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='') as prompt, redirect_stdout(io.StringIO()):
            self.assertTrue(keys.choose_for_launch(profiles[0], profiles, api))
        self.assertIn('[2]', prompt.call_args.args[0])
        self.assertEqual(api.get_codex_secret(profiles[0]), 'synthetic-second-key')

    def test_cancel_leaves_selection_and_credentials_unchanged(self):
        self.add()
        original = api.CODEX_PROFILES_PATH.read_bytes()
        with patch.object(keys.sys.stdin, 'isatty', return_value=True), patch('builtins.input', return_value='q'), redirect_stdout(io.StringIO()):
            self.assertIsNone(api.resolve_codex_api_key(self.current(), keys.registry_profiles(api)))
        self.assertEqual(api.CODEX_PROFILES_PATH.read_bytes(), original)

    def test_single_key_and_noninteractive_launches_do_not_prompt(self):
        for multi in (False, True):
            if multi:
                self.add()
            with patch.object(keys.sys.stdin, 'isatty', return_value=False), patch('builtins.input', side_effect=AssertionError('unexpected prompt')):
                self.assertEqual(api.resolve_codex_api_key(self.current(), keys.registry_profiles(api)), 'synthetic-first-key')

    def test_selected_missing_key_never_falls_back_or_asks_for_replacement(self):
        self.add()
        profile = keys.use('anyrouter', 'account-2', api)
        reference = profile['credentialId']
        self.store.clear(reference)
        with patch.object(keys.sys.stdin, 'isatty', return_value=False), patch.object(api, 'getpass', side_effect=AssertionError('replacement prompt')), redirect_stderr(io.StringIO()):
            self.assertIsNone(api.resolve_codex_api_key(profile, keys.registry_profiles(api)))
        self.assertEqual(self.current()['credentialId'], reference)
        self.assertEqual(self.store.get('codex:anyrouter'), 'synthetic-first-key')

    def test_duplicate_names_are_rejected_before_secret_prompt(self):
        self.add()
        with patch.object(api, 'getpass') as prompt, redirect_stderr(io.StringIO()):
            self.assertEqual(keys.main(['add', 'anyrouter', 'ACCOUNT-2'], api), 1)
        prompt.assert_not_called()

    def test_storage_failure_retains_original_metadata_and_redacts_exception(self):
        before = api.CODEX_PROFILES_PATH.read_bytes()
        output = io.StringIO()
        with patch.object(api, 'getpass', return_value='synthetic-second-key'), patch.object(self.store, 'set', side_effect=SecureStoreError('sensitive-error-sentinel')), redirect_stderr(output):
            self.assertEqual(keys.main(['add', 'anyrouter', 'account-2'], api), 1)
        self.assertNotIn('sensitive-error-sentinel', output.getvalue())
        self.assertEqual(api.CODEX_PROFILES_PATH.read_bytes(), before)
        self.assertEqual(self.store.get('codex:anyrouter'), 'synthetic-first-key')

    def test_metadata_save_failure_retains_original_key(self):
        before = api.CODEX_PROFILES_PATH.read_bytes()
        with patch.object(api, 'getpass', return_value='synthetic-second-key'), patch.object(api, 'save_codex_profiles', side_effect=OSError('metadata unavailable')), redirect_stderr(io.StringIO()):
            self.assertEqual(keys.main(['add', 'anyrouter', 'account-2'], api), 1)
        self.assertEqual(api.CODEX_PROFILES_PATH.read_bytes(), before)
        self.assertEqual(api.get_codex_secret(self.current()), 'synthetic-first-key')

    def test_list_and_help_are_read_only_and_do_not_read_credentials(self):
        self.add()
        before = api.CODEX_PROFILES_PATH.read_bytes()
        output = io.StringIO()
        with patch.object(self.store, 'get', side_effect=AssertionError('credential read')), patch.object(api, 'getpass', side_effect=AssertionError('prompt')), redirect_stdout(output):
            self.assertEqual(keys.main(['list', 'anyrouter'], api), 0)
            self.assertEqual(api.codex_main(['keys', '--help']), 0)
        self.assertIn('account-2', output.getvalue())
        self.assertNotIn('synthetic-', output.getvalue())
        self.assertEqual(api.CODEX_PROFILES_PATH.read_bytes(), before)

    def test_invalid_active_reference_or_account_profile_cannot_prompt(self):
        profile = self.add()
        profile['credentialId'] = 'unlisted-reference'
        api.save_codex_profiles([profile])
        with patch('builtins.input', side_effect=AssertionError('prompt')), redirect_stderr(io.StringIO()):
            self.assertIsNone(api.resolve_codex_api_key(profile, [profile]))
        profile['type'] = 'chatgpt'
        api.save_codex_profiles([profile])
        with patch.object(api, 'getpass') as prompt, redirect_stderr(io.StringIO()):
            self.assertEqual(keys.main(['add', 'anyrouter', 'another-key'], api), 1)
        prompt.assert_not_called()

    def test_desktop_for_both_keys_uses_identical_history_and_data_paths(self):
        self.add()
        paths = []
        for choice, expected in [('2', 'synthetic-second-key'), ('1', 'synthetic-first-key')]:
            with ExitStack() as stack:
                for name, value in (
                    ('find_codex_desktop_executable', self.root / 'ChatGPT.exe'),
                    ('desktop_environment', {}), ('prepare_codex_vision_runtime', True),
                    ('sync_codex_shared_mcp', None), ('repair_codex_home_images', None),
                    ('add_current_project_trust', None), ('update_codex_last_used', None),
                    ('label_codex_desktop_window', True), ('register_macos_codex_desktop', True),
                ):
                    stack.enter_context(patch.object(api, name, return_value=value))
                keyring = stack.enter_context(patch.object(api, 'ensure_codex_keyring_auth', return_value=True))
                start = stack.enter_context(patch.object(api, 'start_detached_process', return_value=0))
                stack.enter_context(patch.dict(os.environ, {'APICODEX_DREAM_SKIN_SCRIPT': ''}))
                stack.enter_context(patch.object(keys.sys.stdin, 'isatty', return_value=True))
                answers = ['1', choice] if choice == '2' else [choice]
                stack.enter_context(patch('builtins.input', side_effect=answers))
                stack.enter_context(redirect_stdout(io.StringIO()))
                args = ['--desktop'] if choice == '2' else ['--desktop', '--api-profile', 'anyrouter']
                self.assertEqual(api.codex_main(args), 0)
                self.assertEqual(keyring.call_args.args[:2], (self.home, expected))
                self.assertEqual(start.call_args.kwargs['env']['APICODEX_API_KEY'], expected)
                self.assertNotIn(expected, str(start.call_args.args))
                paths.append((start.call_args.args[1], start.call_args.kwargs['env']['CODEX_HOME']))
        self.assertEqual(paths[0], paths[1])
        self.assertEqual((self.home / 'sessions/chat.jsonl').read_bytes(), b'existing conversation')

    def test_existing_update_replaces_only_the_active_named_key(self):
        self.add()
        profile = keys.use('anyrouter', 'account-2', api)
        with patch.object(api, 'getpass', return_value='synthetic-replacement'), patch.object(api, 'sync_codex_shared_mcp'), redirect_stdout(io.StringIO()):
            self.assertEqual(api.codex_main(['--api-add', '--api-profile', 'anyrouter', '--name', 'anyrouter', '--base-url', 'https://example.test/v1', '--model', 'test-model']), 0)
        self.assertEqual(self.current()['credentialId'], profile['credentialId'])
        self.assertEqual(self.store.get(profile['credentialId']), 'synthetic-replacement')
        self.assertEqual(self.store.get('codex:anyrouter'), 'synthetic-first-key')

    def test_profile_unregister_clears_each_named_credential_after_confirmation(self):
        profile = self.add()
        with patch('builtins.input', return_value='YES'), patch.object(self.store, 'clear') as clear, redirect_stdout(io.StringIO()):
            self.assertEqual(api.remove_codex_profile('anyrouter'), 0)
        self.assertEqual({c.args[0] for c in clear.call_args_list}, {e['credentialId'] for e in profile['apiKeys']})
        self.assertEqual(keys.registry_profiles(api), [])
