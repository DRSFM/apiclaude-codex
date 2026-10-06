from __future__ import annotations

import io
import json
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import apiagent


class CodexTpsCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.api_root = self.root / '.codex-api'
        self.home = self.api_root / 'profiles' / 'vision'
        self.home.mkdir(parents=True)
        self.config = self.home / 'config.toml'
        self.state_path = self.root / 'audits' / 'profiles-state.json'
        self.state_path.parent.mkdir()
        self.token = 'a' * 32
        self.endpoint = 'http://127.0.0.1:19876/v1'
        self.upstream = 'http://127.0.0.1:19101/__apicodex_vision__/on-demand/v1'
        self.profile = {
            'id': 'vision', 'name': 'vision', 'home': 'profiles/vision',
            'baseUrl': 'https://example.test/v1',
            'vision': {'enabled': True, 'proxyPort': 19101},
        }
        self.state = {
            'schema_version': 1, 'active': True, 'token': self.token,
            'entries': [{'home': str(self.home), 'key_path': ['model_providers', 'apicodex', 'base_url'],
                         'upstream': self.upstream, 'endpoint': self.endpoint}],
        }
        self.original = (
            f'# CODEX TPS PROFILES AUDIT {self.token}\n'
            '# CODEX TPS STATE ' + json.dumps(str(self.state_path)) + '\n'
            'model_provider = "apicodex"\nmodel = "existing-model"\n'
            'model_reasoning_effort = "high"\n[model_providers.apicodex]\n'
            f'base_url = "{self.endpoint}"\nwire_api = "responses"\nenv_key = "PRIVATE_ENV_NAME"\n'
        )
        self.config.write_text(self.original, encoding='utf-8')
        self.save_state()
        home_patch = patch.object(apiagent, 'CODEX_HOME', self.api_root)
        home_patch.start()
        self.addCleanup(home_patch.stop)

    def save_state(self) -> None:
        self.state_path.write_text(json.dumps(self.state), encoding='utf-8')

    def test_vision_start_preserves_active_capture_after_regenerating_own_address(self) -> None:
        with patch.object(apiagent, 'ensure_codex_vision_worker', return_value=True), \
                patch.object(apiagent.socket, 'create_connection') as connection:
            self.assertTrue(apiagent.prepare_codex_vision_runtime(self.profile))
        text = self.config.read_text(encoding='utf-8')
        self.assertIn(f'base_url = "{self.endpoint}"', text)
        self.assertIn('model = "existing-model"', text)
        self.assertIn('model_reasoning_effort = "high"', text)
        self.assertIn('env_key = "PRIVATE_ENV_NAME"', text)
        self.assertIn('enable_request_compression = false', text)
        self.assertIn('[mcp_servers.apicodex_vision]', text)
        connection.assert_called_once_with(('127.0.0.1', 19876), timeout=1)

    def test_unmanaged_and_stopped_capture_keep_normal_vision_start(self) -> None:
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                self.state['active'] = not stopped
                self.save_state()
                self.config.write_text(self.original if stopped else self.original.split('\n', 2)[2], encoding='utf-8')
                with patch.object(apiagent, 'ensure_codex_vision_worker', return_value=True), \
                        patch.object(apiagent.socket, 'create_connection') as connection:
                    self.assertTrue(apiagent.prepare_codex_vision_runtime(self.profile))
                self.assertIn(f'base_url = "{self.upstream}"', self.config.read_text(encoding='utf-8'))
                connection.assert_not_called()

    def test_invalid_opt_in_or_dead_capture_blocks_launch_without_echoing_values(self) -> None:
        cases = ['token', 'home', 'upstream', 'endpoint', 'dead', 'missing']
        for case in cases:
            with self.subTest(case=case):
                saved = json.loads(json.dumps(self.state))
                self.config.write_text(self.original, encoding='utf-8')
                if case == 'token': self.state['token'] = 'b' * 32
                if case == 'home': self.state['entries'][0]['home'] = str(self.root / 'other')
                if case == 'upstream': self.state['entries'][0]['upstream'] = 'https://changed.test/v1'
                if case == 'endpoint': self.state['entries'][0]['endpoint'] = 'https://outside.test/v1?key=PRIVATE_VALUE'
                self.save_state()
                if case == 'missing': self.state_path.unlink()
                errors = io.StringIO()
                with patch.object(apiagent, 'ensure_codex_vision_worker', return_value=True), \
                        patch.object(apiagent.socket, 'create_connection', side_effect=OSError('PRIVATE_VALUE') if case == 'dead' else None), \
                        redirect_stderr(errors):
                    self.assertFalse(apiagent.prepare_codex_vision_runtime(self.profile))
                self.assertNotIn(f'base_url = "{self.endpoint}"', self.config.read_text(encoding='utf-8'))
                self.assertIn('TPS capture', errors.getvalue())
                self.assertNotIn('PRIVATE_VALUE', errors.getvalue())
                self.state = saved
                self.save_state()
