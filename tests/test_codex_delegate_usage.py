from __future__ import annotations

import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from codex_app_server import AppServerError
from codex_delegate_usage import usage_snapshot
import codex_delegate as delegate
from tests import test_codex_delegate as fixtures

FakeClient = fixtures.FakeClient


def payload(used=0, weekly=0):
    return {'ordinaryUsageAllowed': True, 'rateLimits': {'limitId': 'codex', 'planType': 'plus',
        'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': 3000},
        'secondary': {'usedPercent': weekly, 'windowDurationMins': 10080, 'resetsAt': 9000}}}


class UsageSnapshotTests(unittest.TestCase):
    def parse(self, data):
        return usage_snapshot(data, 'b', timestamp=1000)

    def test_boundary_for_both_windows_and_overuse(self):
        for used, weekly, allowed in [(89, 89, True), (90, 0, False), (0, 90, False), (101, 0, False)]:
            with self.subTest(used=used, weekly=weekly):
                result = self.parse(payload(used, weekly))
                self.assertEqual(result['guard']['allowNewCalls'], allowed)
                self.assertEqual(result['guard']['activeTaskPolicy'], 'finish')

    def test_unknown_malformed_partial_or_past_reset_does_not_authorize(self):
        bad = [None, {}, {'ordinaryUsageAllowed': True}]
        for key, value in [('usedPercent', True), ('usedPercent', -1), ('usedPercent', float('nan')),
                           ('windowDurationMins', None), ('resetsAt', 999)]:
            data = payload()
            data['rateLimits']['primary'][key] = value
            bad.append(data)
        data = payload()
        data['rateLimits']['secondary'] = None
        bad.append(data)
        data = payload()
        data['ordinaryUsageAllowed'] = None
        bad.append(data)
        for data in bad:
            with self.subTest(data=data):
                result = self.parse(data)
                self.assertFalse(result['guard']['allowNewCalls'])
                self.assertFalse(result['available'])

    def test_service_block_and_multi_bucket_conservative_reserve(self):
        data = payload()
        data['ordinaryUsageAllowed'] = False
        self.assertEqual(self.parse(data)['guard']['reason'], 'service_blocked')
        data = payload()
        data['rateLimits']['spendControlReached'] = True
        self.assertFalse(self.parse(data)['guard']['allowNewCalls'])
        data = payload()
        data['rateLimitsByLimitId'] = {'codex': copy.deepcopy(data['rateLimits']),
                                     'model-specific': {**payload(95)['rateLimits'], 'limitId': 'model-specific'}}
        parsed = self.parse(data)
        self.assertEqual(len(parsed['buckets']), 2)
        self.assertEqual(parsed['guard']['reason'], 'reserve_reached')
        self.assertEqual(parsed['buckets'][1]['windows'][0]['remainingPercent'], 5)

    def test_duration_not_primary_secondary_position_defines_window(self):
        data = payload()
        a, b = data['rateLimits']['primary'], data['rateLimits']['secondary']
        data['rateLimits'].update(primary=b, secondary=a)
        result = self.parse(data)
        self.assertEqual([w['label'] for w in result['buckets'][0]['windows']], ['7d', '5h'])
        self.assertTrue(result['guard']['allowNewCalls'])


class UsageGuardTests(unittest.TestCase):
    setUp = fixtures.DelegateTests.setUp
    make_manager = fixtures.DelegateTests.make_manager
    spawn = fixtures.DelegateTests.spawn

    def test_usage_tool_reads_without_model_turn_and_rejects_other_account(self):
        server = delegate.DelegateMcpServer(self.manager)
        response = server.handle({'id': 1, 'method': 'tools/call', 'params': {'name': 'get_usage', 'arguments': {}}})
        self.assertFalse(response['result']['isError'])
        self.assertTrue(response['result']['structuredContent']['guard']['allowNewCalls'])
        client = FakeClient.instances[-1]
        self.assertEqual([m for m, _ in client.requests], ['account/rateLimits/read'])
        self.assertTrue(client.closed)
        with self.assertRaises(delegate.DelegateError):
            self.manager.get_usage(account='not-enabled')
        self.assertFalse(list(self.manager.state.glob('*.json')))

    def test_spawn_block_does_not_create_official_thread_and_can_retry_after_fresh_recovery(self):
        original = FakeClient.request
        def low(client, method, params, **kwargs):
            client.quota_used = 90
            return original(client, method, params, **kwargs)
        with patch.object(FakeClient, 'request', low):
            result = self.spawn(context='none')
        self.assertEqual(result['status'], 'quota_blocked')
        self.assertNotIn('threadId', result)
        self.assertFalse(any(m.startswith('thread/') or m.startswith('turn/') for m, _ in FakeClient.instances[-1].requests))
        self.assertTrue(FakeClient.instances[-1].closed)
        self.assertTrue(Path(result['artifacts']).exists())
        retry = self.spawn(context='none')
        self.assertEqual(retry['status'], 'running')
        self.assertEqual(retry['taskId'], result['taskId'])

    def test_running_work_finishes_while_steering_is_blocked_and_interrupt_stays_available(self):
        result = self.spawn(context='none')
        client = FakeClient.instances[-1]
        client.quota_used = 95
        usage = self.manager.get_usage()
        self.assertFalse(usage['guard']['allowNewCalls'])
        self.assertEqual(len(FakeClient.instances), 1)
        blocked = self.manager.send_message(self.owner, result['taskId'], 'more work')
        self.assertEqual(blocked['status'], 'quota_blocked')
        self.assertEqual(blocked['taskStatus'], 'running')
        self.assertFalse(blocked['accepted'])
        self.assertFalse(client.closed)
        self.assertNotIn('turn/steer', [m for m, _ in client.requests])
        self.assertNotIn('turn/interrupt', [m for m, _ in client.requests])
        client.complete('kept result')
        completed = self.manager.wait(self.owner, result['taskId'], 0)
        self.assertEqual(completed['answer'], 'kept result')
        self.assertEqual(completed['status'], 'completed')
        another = self.manager.spawn(self.owner, message='new', request_id='new', context='none')
        FakeClient.instances[-1].quota_used = 99
        self.assertEqual(self.manager.interrupt(self.owner, another['taskId'])['status'], 'interrupted')

    def test_resume_block_preserves_completed_thread_answer_and_releases_lease(self):
        result = self.spawn(context='none')
        FakeClient.instances[-1].complete('original answer')
        self.manager.wait(self.owner, result['taskId'], 0)
        original = FakeClient.request
        def low(client, method, params, **kwargs):
            client.quota_used = 90
            return original(client, method, params, **kwargs)
        with patch.object(FakeClient, 'request', low):
            blocked = self.manager.send_message(self.owner, result['taskId'], 'next turn')
        self.assertEqual(blocked['status'], 'quota_blocked')
        self.assertEqual(blocked['taskStatus'], 'completed')
        self.assertTrue(FakeClient.instances[-1].closed)
        self.assertNotIn('thread/resume', [m for m, _ in FakeClient.instances[-1].requests])
        saved = self.manager.inspect(self.owner, result['taskId'])
        self.assertEqual(saved['answer'], 'original answer')
        self.assertEqual(saved['threadId'], result['threadId'])
        continued = self.manager.send_message(self.owner, result['taskId'], 'next turn')
        self.assertEqual(continued['status'], 'running')
        self.assertEqual(continued['threadId'], result['threadId'])

    def test_query_failure_is_closed_and_redacted_while_task_controls_keep_working(self):
        original = FakeClient.request
        def unavailable(client, method, params, **kwargs):
            if method == 'account/rateLimits/read':
                raise AppServerError('access_token=synthetic-secret')
            return original(client, method, params, **kwargs)
        with patch.object(FakeClient, 'request', unavailable):
            usage = self.manager.get_usage()
            result = self.spawn(context='none')
        self.assertFalse(usage['available'])
        self.assertEqual(result['status'], 'quota_blocked')
        self.assertEqual(result['usage']['guard']['reason'], 'usage_unavailable')
        self.assertNotIn('synthetic-secret', json.dumps([usage, result]))
        self.assertTrue(all(c.closed for c in FakeClient.instances))

    def test_standalone_query_start_failure_closes_client_and_returns_unknown(self):
        with patch.object(FakeClient, 'start', side_effect=AppServerError('synthetic startup failure')):
            result = self.manager.get_usage()
        self.assertFalse(result['guard']['allowNewCalls'])
        self.assertEqual(result['guard']['reason'], 'usage_unavailable')
        self.assertTrue(FakeClient.instances[-1].closed)
