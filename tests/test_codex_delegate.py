from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import apiagent as api
import codex_accounts as accounts
from codex_app_server import AppServerError
import codex_delegate as delegate
from codex_delegate_runtime import DelegateClient


class FakeClient:
    instances = []

    def __init__(self, home, **kwargs):
        self.home, self.kwargs = home, kwargs
        self.requests = []
        self.events = []
        self.dead = False
        self.closed = False
        self.thread = str(uuid.uuid4())
        self.turn = None
        self.instances.append(self)

    def start(self):
        pass

    def request(self, method, params, **kwargs):
        self.requests.append((method, params))
        if method in {'thread/start', 'thread/fork'}:
            return {'thread': {'id': self.thread}}
        if method == 'turn/start':
            self.turn = str(uuid.uuid4())
            return {'turn': {'id': self.turn}}
        if method == 'turn/interrupt':
            self.events.append({'method': 'turn/completed', 'params': {'turn': {'id': self.turn, 'status': 'interrupted'}}})
        return {}

    def complete(self, text='done', status='completed'):
        self.events += [
            {'method': 'item/completed', 'params': {'item': {'type': 'agentMessage', 'text': text}}},
            {'method': 'turn/completed', 'params': {'turn': {'id': self.turn, 'status': status}}},
        ]

    def drain(self):
        events, self.events = self.events, []
        return events

    def close(self):
        self.closed = True


class FakeSource:
    def __init__(self, home, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read_thread(self, owner, **kwargs):
        return {'id': owner, 'turns': [{'id': 't1', 'status': 'completed', 'items': [
            {'type': 'userMessage', 'content': [{'type': 'text', 'text': 'The reference is BLUE-7312.'}]},
            {'type': 'agentMessage', 'text': 'Recorded.'},
            {'type': 'reasoning', 'text': 'hidden-canary'},
        ]}]}


class DelegateTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for key, value in {'HOME': self.root, 'CODEX_HOME': self.root / 'api',
                           'CODEX_PROFILES_PATH': self.root / 'api' / 'profiles.json',
                           'CODEX_DESKTOP_DATA_ROOT': self.root / 'desktop'}.items():
            p = patch.object(api, key, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(api, 'sync_codex_shared_mcp')
        p.start()
        self.addCleanup(p.stop)
        self.profile = accounts.create_profile('b', 'gpt-test', api)
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'config.toml').write_text('model = "parent-model"\n')
        self.work = self.root / 'work'
        self.work.mkdir()
        self.owner = str(uuid.uuid4())
        self.config = {'sourceHome': str(self.source), 'workspaces': [str(self.work)],
                       'targetIds': [self.profile['id']], 'sandbox': 'read-only'}
        FakeClient.instances = []
        self.manager = self.make_manager()

    def make_manager(self):
        manager = delegate.DelegateManager(self.config, api, client_factory=FakeClient,
                    source_factory=FakeSource, executable='synthetic-codex')
        self.addCleanup(manager.close)
        return manager

    def spawn(self, **kwargs):
        return self.manager.spawn(self.owner, message='Find the reference.', request_id='task-1', **kwargs)

    def test_context_target_binding_result_archive_and_idempotency(self):
        result = self.spawn()
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['accountId'], self.profile['id'])
        self.assertEqual(result['context']['mode'], 'official_visible_history')
        client = FakeClient.instances[-1]
        self.assertEqual(client.home, accounts.profile_home(self.profile, api))
        prompt = next(params['input'][0]['text'] for method, params in client.requests if method == 'turn/start')
        self.assertIn('BLUE-7312', prompt)
        self.assertNotIn('hidden-canary', prompt)
        self.assertIn('agents.enabled=false', client.kwargs['config_overrides'])
        repeat = self.spawn()
        self.assertEqual(result['taskId'], repeat['taskId'])
        self.assertEqual(len(FakeClient.instances), 1)
        with self.assertRaises(delegate.DelegateError):
            self.manager.spawn(self.owner, message='Different task', request_id='task-1')
        client.complete('BLUE-7312')
        done = self.manager.wait(self.owner, result['taskId'], 0)
        self.assertEqual(done['status'], 'completed')
        self.assertEqual(done['answer'], 'BLUE-7312')
        self.assertTrue(client.closed)
        self.assertEqual((Path(done['artifacts']) / '结果.md').read_text(), 'BLUE-7312')
        self.assertFalse(list((client.home / '.apicodex-runs').glob('*.json')))

    def test_native_legacy_fork_is_preferred(self):
        path = self.source / 'sessions' / 'sample.jsonl'
        path.parent.mkdir()
        path.write_text(json.dumps({'payload': {'history_mode': 'legacy'}}) + '\n')
        read = FakeSource.read_thread
        def source(instance, owner, **kwargs):
            return {**read(instance, owner, **kwargs), 'path': str(path)}
        with patch.object(FakeSource, 'read_thread', source):
            result = self.spawn()
        self.assertEqual(result['context']['mode'], 'official_fork')
        self.assertIn('thread/fork', [method for method, _ in FakeClient.instances[-1].requests])

    def test_no_context_is_explicit_and_does_not_read_parent(self):
        with patch.object(FakeSource, 'read_thread', side_effect=AssertionError('read parent')):
            result = self.spawn(context='none')
        self.assertEqual(result['context'], {'mode': 'none'})

    def test_wrong_owner_and_disallowed_workspace_account_are_rejected(self):
        result = self.spawn()
        stranger = str(uuid.uuid4())
        self.assertEqual(self.manager.list_tasks(stranger), {'tasks': []})
        for method, args in [('inspect', (result['taskId'],)), ('send_message', (result['taskId'], 'hijack')),
                             ('interrupt', (result['taskId'],))]:
            with self.assertRaises(delegate.DelegateError):
                getattr(self.manager, method)(stranger, *args)
        with self.assertRaises(delegate.DelegateError):
            self.manager.spawn(self.owner, message='outside', request_id='outside', cwd=str(self.root))
        with self.assertRaises(delegate.DelegateError):
            self.manager.spawn(self.owner, message='wrong account', request_id='wrong', account='unknown')

    def test_followup_steers_running_then_resumes_same_account_thread(self):
        result = self.spawn()
        first = FakeClient.instances[-1]
        self.manager.send_message(self.owner, result['taskId'], 'Also check X.')
        self.assertIn('turn/steer', [method for method, _ in first.requests])
        first.complete()
        self.manager.wait(self.owner, result['taskId'], 0)
        restarted = self.make_manager()
        continued = restarted.send_message(self.owner, result['taskId'], 'Now explain Y.')
        second = FakeClient.instances[-1]
        self.assertEqual(continued['threadId'], result['threadId'])
        self.assertEqual(second.home, first.home)
        self.assertEqual(next(p['threadId'] for m, p in second.requests if m == 'thread/resume'), result['threadId'])

    def test_interrupt_confirms_terminal_status_and_releases_account(self):
        result = self.spawn()
        cancelled = self.manager.interrupt(self.owner, result['taskId'])
        self.assertEqual(cancelled['status'], 'interrupted')
        another = self.manager.spawn(self.owner, message='next', request_id='next', context='none')
        self.assertEqual(another['status'], 'running')

    def test_concurrent_controllers_cannot_run_same_account_and_no_fallback(self):
        self.spawn()
        other = self.make_manager()
        failed = other.spawn(str(uuid.uuid4()), message='collision', request_id='collision', context='none')
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(len(FakeClient.instances), 1)

    def test_context_size_and_empty_context_fail_without_summary_fallback(self):
        with self.assertRaises(delegate.DelegateError):
            delegate.visible_context({'turns': []})
        with self.assertRaises(delegate.DelegateError):
            delegate.visible_context({'turns': [{'items': [{'type': 'agentMessage', 'text': 'x' * (delegate.MAX_CONTEXT + 1)}]}]})

    def test_retry_from_second_controller_does_not_duplicate_or_overwrite_active_task(self):
        other = self.make_manager()
        result = self.spawn(context='none')
        duplicate = other.spawn(self.owner, message='Find the reference.', request_id='task-1', context='none')
        self.assertEqual(duplicate['taskId'], result['taskId'])
        self.assertEqual(len(FakeClient.instances), 1)
        with self.assertRaises(delegate.DelegateError):
            other.send_message(self.owner, result['taskId'], 'continue')
        saved = json.loads(self.manager.tasks[result['taskId']].path.read_text(encoding='utf-8'))
        self.assertEqual(saved['status'], 'running')
        FakeClient.instances[-1].complete('finished once')
        self.manager.wait(self.owner, result['taskId'], 0)
        duplicate = other.spawn(self.owner, message='Find the reference.', request_id='task-1', context='none')
        self.assertEqual(duplicate['status'], 'completed')
        self.assertEqual(len(FakeClient.instances), 1)

    def test_uncertain_followup_start_releases_runtime_without_repeating(self):
        result = self.spawn(context='none')
        FakeClient.instances[-1].complete()
        self.manager.wait(self.owner, result['taskId'], 0)
        original = FakeClient.request
        def fail_turn(client, method, params, **kwargs):
            if method == 'turn/start':
                raise AppServerError('Synthetic timeout')
            return original(client, method, params, **kwargs)
        with patch.object(FakeClient, 'request', fail_turn):
            with self.assertRaises(delegate.DelegateError):
                self.manager.send_message(self.owner, result['taskId'], 'follow up')
        self.assertTrue(FakeClient.instances[-1].closed)
        self.assertNotIn(self.manager.inspect(self.owner, result['taskId'])['status'], delegate.ACTIVE)
        self.assertEqual(self.manager.spawn(self.owner, message='new', request_id='new', context='none')['status'], 'running')

    def test_runtime_failure_retains_record_redacts_error_and_releases_lease(self):
        with patch.object(FakeClient, 'request', side_effect=AppServerError('token=synthetic-secret')):
            result = self.spawn(context='none')
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('synthetic-secret', json.dumps(result))
        self.assertTrue(FakeClient.instances[-1].closed)
        self.assertTrue(Path(result['artifacts']).is_dir())

    def test_approval_is_attention_not_success(self):
        result = self.spawn(context='none')
        client = FakeClient.instances[-1]
        client.events.append({'method': 'delegate/needsAttention', 'params': {}})
        client.complete('I could not proceed.')
        done = self.manager.wait(self.owner, result['taskId'], 0)
        self.assertEqual(done['status'], 'needs_attention')

    def test_mcp_metadata_validation_and_roundtrip(self):
        server = delegate.DelegateMcpServer(self.manager)
        base = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call'}
        tools = server.handle({'id': 2, 'method': 'tools/list'})['result']['tools']
        self.assertEqual(len(tools), 6)
        missing = server.handle({**base, 'params': {'name': 'spawn', 'arguments': {'message': 'hi', 'request_id': 'one'}}})
        self.assertTrue(missing['result']['isError'])
        good = server.handle({**base, 'params': {'name': 'spawn', '_meta': {'threadId': self.owner},
            'arguments': {'message': 'hi', 'request_id': 'one', 'context': 'none'}}})
        self.assertFalse(good['result']['isError'])
        self.assertEqual(good['result']['structuredContent']['accountId'], self.profile['id'])
        bad = server.handle({**base, 'params': {'name': 'spawn', '_meta': {'threadId': self.owner},
            'arguments': {'message': 123, 'request_id': 'one'}}})
        self.assertTrue(bad['result']['isError'])

    def test_install_preview_backup_and_unrelated_configuration(self):
        path = self.source / 'config.toml'
        original = path.read_bytes()
        ns = argparse.Namespace(source_home=self.source, target_account=['b'], workspace=[self.work], sandbox='read-only', dry_run=True)
        report = delegate.install(ns, api)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(Path(report['config']).exists())
        ns.dry_run = False
        result = delegate.install(ns, api)
        self.assertEqual(Path(result['backup']).read_bytes(), original)
        self.assertIn('model = "parent-model"', path.read_text())
        self.assertIn('[mcp_servers.apicodex_delegate]', path.read_text())
        delegate.install(ns, api)
        self.assertEqual(path.read_text().count('[mcp_servers.apicodex_delegate]'), 1)
        self.assertEqual(json.loads(Path(result['config']).read_text())['targetIds'], [self.profile['id']])

    def test_tool_trust_requires_explicit_install_option_and_preserves_sandbox(self):
        ns = argparse.Namespace(source_home=self.source, target_account=['b'], workspace=[self.work],
                                sandbox='read-only', dry_run=False, trust_tools=False)
        delegate.install(ns, api)
        path = self.source / 'config.toml'
        self.assertNotIn('approval_mode', path.read_text())
        ns.trust_tools, ns.dry_run = True, True
        preview = delegate.install(ns, api)
        self.assertEqual(preview['trustedTools'], ['spawn', 'send_message', 'interrupt'])
        self.assertNotIn('approval_mode', path.read_text())
        ns.dry_run = False
        delegate.install(ns, api)
        self.assertEqual(path.read_text().count('approval_mode = "approve"'), 3)
        self.assertEqual(json.loads(Path(preview['config']).read_text())['sandbox'], 'read-only')
        ns.trust_tools = False
        delegate.install(ns, api)
        self.assertNotIn('approval_mode', path.read_text())


class DelegateTransportTests(unittest.TestCase):
    def test_notifications_progress_without_an_outstanding_request_and_approval_is_not_granted(self):
        messages = [
            {'method': 'item/reasoning/textDelta', 'params': {'delta': 'hidden'}},
            {'id': 7, 'method': 'item/commandExecution/requestApproval', 'params': {'command': 'sensitive-command'}},
            {'method': 'turn/completed', 'params': {'turn': {'id': 't', 'status': 'completed'}}},
        ]
        client = DelegateClient(Path.cwd())
        output = io.StringIO()
        client._process = SimpleNamespace(stdout=io.StringIO('\n'.join(json.dumps(m) for m in messages)),
                                         stdin=output, poll=lambda: None)
        client._read_stdout()
        events = client.drain()
        self.assertEqual([e['method'] for e in events], ['delegate/needsAttention', 'turn/completed'])
        reply = json.loads(output.getvalue())
        self.assertIn('error', reply)
        self.assertNotIn('result', reply)
        self.assertNotIn('sensitive-command', json.dumps(events))
        with self.assertRaises(AppServerError):
            client.request('thread/read', {})


@unittest.skipUnless(os.environ.get('APICODEX_TEST_NATIVE_DELEGATE'), 'explicit isolated official runtime validation')
class NativeDelegateTests(unittest.TestCase):
    setUp = DelegateTests.setUp
    make_manager = DelegateTests.make_manager
    # Only synthetic histories and empty temporary account homes; no turns or
    # model requests are started in these interface compatibility checks.
    def test_official_legacy_fork_and_paginated_context_adapter(self):
        from codex_app_server import CodexAppServer
        from tests.test_codex_paginated_import import modern_rows
        from tests.test_codex_conversation_pool import write_rollout
        executable = os.environ['APICODEX_TEST_NATIVE_DELEGATE']
        self.manager.close()
        self.manager = delegate.DelegateManager(self.config, api, executable=executable)
        self.addCleanup(self.manager.close)
        for mode in ('legacy', 'paginated'):
            rows = modern_rows(self.work)
            tid = str(uuid.uuid4())
            rows[0]['payload'].update(id=tid, session_id=tid, cwd=str(self.work), model_provider='openai',
                history_mode=mode, base_instructions={'text': 'Synthetic context'}, dynamic_tools=[], git=None)
            if mode == 'legacy':
                rows[-1:-1] = [
                    {'timestamp': '2026-01-01T00:00:02Z', 'type': 'event_msg', 'payload': {
                        'type': 'user_message', 'message': '完整问题', 'images': [], 'local_images': []}},
                    {'timestamp': '2026-01-01T00:00:02Z', 'type': 'event_msg', 'payload': {
                        'type': 'agent_message', 'message': '完整回答', 'phase': 'final_answer'}},
                ]
            for i, row in enumerate(rows):
                row['ordinal'] = i
                if 'thread_id' in row['payload']:
                    row['payload']['thread_id'] = tid
            path = self.source / 'sessions' / f'rollout-2026-01-01T00-00-00-{tid}.jsonl'
            write_rollout(path, rows)
            with CodexAppServer(self.source, codex_command=executable) as source:
                source.request('thread/resume', {'threadId': tid, 'path': str(path),
                    'cwd': str(self.work), 'model': 'gpt-test', 'modelProvider': 'openai',
                    'excludeTurns': True, 'deferGoalContinuation': True})
            folder = self.work / '子代理任务' / mode
            folder.mkdir(parents=True)
            task = delegate.Task({'owner': tid, 'cwd': str(self.work), 'model': 'gpt-test', 'artifacts': str(folder)}, folder/'record.json')
            try:
                self.manager._open(task, self.profile)
                thread_id, prefix = self.manager._thread(task, 'auto')
                self.assertNotEqual(thread_id, tid)
                if mode == 'legacy':
                    self.assertEqual(task.record['context']['mode'], 'official_fork')
                    actual = task.client.read_thread(thread_id, include_turns=True)
                    self.assertTrue(actual['turns'])
                else:
                    self.assertEqual(task.record['context']['mode'], 'official_visible_history')
                    self.assertIn('完整问题', prefix)
                    self.assertIn('完整回答', prefix)
                    self.assertIn('commandExecution', prefix)
            finally:
                self.manager._release(task)
