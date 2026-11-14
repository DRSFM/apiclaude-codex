"""Normalize official quota snapshots and decide whether a new call may start."""
from __future__ import annotations

from datetime import datetime, timezone
import math
import time
from typing import Any

RESERVE_PERCENT = 10
WINDOWS = {300: '5h', 10080: '7d'}


def usage_snapshot(payload: Any, account_id: str, *, reserve: int = RESERVE_PERCENT,
                   timestamp: float | None = None) -> dict[str, Any]:
    """Fail closed on incomplete data; a reset timestamp is never a refill."""
    timestamp = time.time() if timestamp is None else timestamp
    result: dict[str, Any] = {
        'accountId': account_id,
        'queriedAt': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        'available': False, 'ordinaryUsageAllowed': None, 'buckets': [],
        'guard': {'reservePercent': reserve, 'allowNewCalls': False,
                  'reason': 'usage_unavailable', 'activeTaskPolicy': 'finish',
                  'scope': 'all_reported_5h_and_7d_windows'},
    }
    if not isinstance(payload, dict):
        return result
    ordinary = payload.get('ordinaryUsageAllowed')
    result['ordinaryUsageAllowed'] = ordinary if type(ordinary) is bool else None
    raw_buckets: list[tuple[str, Any]] = []
    primary = payload.get('rateLimits')
    if isinstance(primary, dict) and primary:
        raw_buckets.append(('default', primary))
    multi = payload.get('rateLimitsByLimitId')
    if isinstance(multi, dict):
        raw_buckets.extend((str(key), value) for key, value in multi.items())
    seen, durations = set(), set()
    incomplete = (not raw_buckets or type(ordinary) is not bool
                  or (multi is not None and not isinstance(multi, dict)))
    reserve_reached = False
    service_blocked = ordinary is False
    for key, raw in raw_buckets:
        if not isinstance(raw, dict):
            incomplete = True
            continue
        limit_id = raw.get('limitId') or key
        # The default bucket is often repeated verbatim in the multi-bucket map.
        marker = (str(limit_id), repr(raw))
        if marker in seen:
            continue
        seen.add(marker)
        bucket: dict[str, Any] = {'limitId': str(limit_id)[:160], 'windows': []}
        if isinstance(raw.get('planType'), str):
            bucket['planType'] = raw['planType'][:80]
        blocked = bool(raw.get('rateLimitReachedType')) or raw.get('spendControlReached') is True
        service_blocked |= blocked
        bucket['serviceBlocked'] = blocked
        for slot in ('primary', 'secondary'):
            window = raw.get(slot)
            if window is None:
                continue
            if not isinstance(window, dict):
                incomplete = True
                continue
            minutes, used, reset = (window.get(k) for k in ('windowDurationMins', 'usedPercent', 'resetsAt'))
            if (type(minutes) is not int or minutes <= 0
                    or type(used) not in (int, float) or not math.isfinite(used) or used < 0):
                incomplete = True
                continue
            if reset is not None and (type(reset) is not int or reset <= 0):
                incomplete = True
                reset = None
            remaining = max(0.0, 100.0 - used)
            bucket['windows'].append({'slot': slot, 'label': WINDOWS.get(minutes, f'{minutes}m'),
                'windowDurationMins': minutes, 'usedPercent': used,
                'remainingPercent': remaining, 'resetsAt': reset})
            if minutes in WINDOWS:
                durations.add(minutes)
                reserve_reached |= remaining <= reserve
                # A server snapshot that has not rolled over cannot authorize
                # new spending just because its reset time has passed.
                if reset is not None and reset <= timestamp:
                    incomplete = True
        result['buckets'].append(bucket)
    if durations != set(WINDOWS):
        incomplete = True
    result['available'] = not incomplete
    reason = ('service_blocked' if service_blocked else 'reserve_reached' if reserve_reached
              else 'usage_unavailable' if incomplete else None)
    result['guard'].update(allowNewCalls=reason is None, reason=reason)
    return result


def blocked_message(snapshot: dict[str, Any]) -> str:
    reason = snapshot['guard']['reason']
    if reason == 'reserve_reached':
        return (f"Quota reserve reached (remaining <= {snapshot['guard']['reservePercent']}% in a reported 5h/7d window). "
                'New calls are paused; running work may finish. Query usage again before continuing.')
    if reason == 'service_blocked':
        return 'The official service reports a usage restriction. New calls are paused; query usage again before continuing.'
    return 'Current quota could not be verified. New calls are paused; running work may finish. Query usage again before continuing.'
