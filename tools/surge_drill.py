#!/usr/bin/env python3
"""Analyze ECS surge observations offline; authentication remains the producer's job."""
from __future__ import annotations

import argparse
import json
import re
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlparse

from capacity_observations import _count, _number

SURFACES = ('map', 'tile', 'query')
MINUTE_SECONDS = 60


def require(condition, message):
    if not condition:
        raise ValueError(message)


def count(value):
    _number(value)
    require(isinstance(value, int), 'counts must be integers')
    return value


def bucket_metrics(buckets):
    for index, bucket in enumerate(buckets):
        require(count(bucket['offsetSeconds']) == index * MINUTE_SECONDS, 'request coverage gap or overlap')
        require(count(bucket['durationSeconds']) == MINUTE_SECONDS, 'request interval must be one minute')
        require(set(bucket['publicRequests']) == set(SURFACES), 'unexpected public request surface')
        requests = sum(_count(bucket['publicRequests'][name]) for name in SURFACES)
        require(count(bucket['errors']) <= requests, 'errors exceed requests')
        count(bucket['originRequests'])
        require(count(bucket['cacheHits']) <= _count(bucket['cacheRequests']) <= requests,
                'cache counts exceed request population')
        _number(bucket['p95LatencyMs'])
        _count(bucket['tasks'])
        require(count(bucket['databaseConnections']) < _count(bucket['databaseConnectionLimit']),
                'database connection limit reached')
        require(count(bucket['taskConnectionFailures']) == 0, 'task failed to acquire connections')


def freshness(edits, duration):
    require(10 <= len(edits) <= duration // MINUTE_SECONDS - 1,
            'ten-minute ramp needs contiguous edit observations')
    delays = []
    for index, edit in enumerate(edits, start=1):
        require(count(edit['committedOffsetSeconds']) == index * MINUTE_SECONDS, 'edit coverage gap or overlap')
        lifetime = _number(edit['cacheLifetimeSeconds'], low=1)
        delay = _number(edit['visibleOffsetSeconds']) - edit['committedOffsetSeconds']
        require(0 <= delay <= lifetime, 'edit visibility exceeds configured cache lifetime')
        require(edit['visibleOffsetSeconds'] <= duration, 'edit visibility was not observed during run')
        _count(edit['afterLifetimeRequests'])
        require(edit['committedOffsetSeconds'] + lifetime <= _number(edit['lastProbeOffsetSeconds']) <= duration,
                'no observed probe after cache lifetime')
        require(count(edit['staleAfterLifetime']) == 0, 'stale content after cache lifetime')
        delays.append(delay)
    return max(delays)


def private_and_drain(run, default_minimum):
    duration = len(run['buckets']) * MINUTE_SECONDS
    drain = run['drain']
    require(count(drain['startedOffsetSeconds']) == duration, 'drain must follow load collection')
    end = count(drain['endedOffsetSeconds'])
    require(end > duration and (end - duration) % MINUTE_SECONDS == 0, 'invalid drain interval')
    require(_count(drain['finalTasks']) == default_minimum, 'default task count not restored')
    require(len(drain['buckets']) == (end - duration) // MINUTE_SECONDS, 'drain coverage gap')
    for index, probe in enumerate(drain['buckets']):
        require(count(probe['offsetSeconds']) == duration + index * MINUTE_SECONDS and
                count(probe['durationSeconds']) == MINUTE_SECONDS, 'drain coverage gap or overlap')
        _count(probe['requests'])
        require(count(probe['errors']) == 0, 'requests failed during task drain')
    probes = run['privateProbes']
    require(len(probes) == end // MINUTE_SECONDS, 'private probe coverage gap')
    for index, probe in enumerate(probes):
        require(count(probe['offsetSeconds']) == index * MINUTE_SECONDS and
                count(probe['durationSeconds']) == MINUTE_SECONDS, 'private probe coverage gap or overlap')
        for field in ('ownerRequests', 'otherUserRequests', 'anonymousRequests'):
            _count(probe[field])
        for field in ('ownerContentMissing', 'crossUserLeaks', 'anonymousLeaks'):
            require(count(probe[field]) == 0, 'private content missing or leaked')


def workload(run, policy):
    buckets = run['buckets']
    schedule = policy['rampMultipliers']
    require(len(schedule) == 10, 'ten ramp multipliers required')
    previous = 1
    for index, multiplier in enumerate(schedule, start=1):
        require(_number(multiplier) > previous, 'ramp must increase each minute')
        for name in SURFACES:
            require(buckets[index]['publicRequests'][name] ==
                    buckets[0]['publicRequests'][name] * Fraction(str(multiplier)),
                    'observed requests do not match registered ramp')
        previous = multiplier
    require(previous >= 100, 'registered ramp must reach 100x')
    serving = run['newTaskServingOffsetSeconds']
    require(buckets[int(serving // MINUTE_SECONDS)]['tasks'] > run['minimumTasks'],
            'new-task serving event lacks scaled task observation')
    return [b['publicRequests'] for b in buckets]


def import_impact(run):
    job = run['import']
    start, end = _number(job['startedOffsetSeconds']), _number(job['endedOffsetSeconds'])
    require(MINUTE_SECONDS <= start < end <= len(run['buckets']) * MINUTE_SECONDS,
            'import must complete during the spike')
    require(job['result'] == 'succeeded', 'background import did not succeed')
    before = [b['p95LatencyMs'] for b in run['buckets'] if b['offsetSeconds'] + MINUTE_SECONDS <= start]
    during = [b['p95LatencyMs'] for b in run['buckets']
              if b['offsetSeconds'] < end and b['offsetSeconds'] + MINUTE_SECONDS > start]
    return max(during) - max(before)


def measure_run(run, policy, default_minimum):
    buckets = run['buckets']
    require(isinstance(buckets, list) and len(buckets) >= 12, 'baseline, ten-minute ramp and hold required')
    bucket_metrics(buckets)
    require(buckets[0]['tasks'] == _count(run['minimumTasks']), 'run did not start at configured minimum')
    require(max(b['tasks'] for b in buckets) > run['minimumTasks'], 'no scale-out observed')
    alarm, serving = _number(run['scalingAlarmOffsetSeconds']), _number(run['newTaskServingOffsetSeconds'])
    require(MINUTE_SECONDS <= alarm <= serving < len(buckets) * MINUTE_SECONDS, 'invalid scale-out timing')
    growth = [b['publicRequests'][name] / buckets[0]['publicRequests'][name]
              for b in buckets[10:] for name in SURFACES]
    require(min(growth) >= 100, 'each public surface must reach and sustain 100x within ten minutes')
    base_origin = _count(buckets[0]['originRequests'])
    origin_growth = max(b['originRequests'] for b in buckets[1:]) / base_origin
    require(origin_growth / min(growth) <= policy['maximumOriginGrowthFraction'], 'origin carries too much spike')
    errors = [b['errors'] / sum(b['publicRequests'].values()) for b in buckets]
    require(max(errors) <= policy['maximumErrorRate'], 'client error rate exceeds policy')
    private_and_drain(run, default_minimum)
    return {
        'clientGrowth': min(growth), 'originGrowth': origin_growth,
        'cacheHitRatio': sum(b['cacheHits'] for b in buckets) / sum(b['cacheRequests'] for b in buckets),
        'maximumMinuteErrorRate': max(errors), 'scaleOutSeconds': serving - alarm,
        'peakTasks': max(b['tasks'] for b in buckets),
        'maximumDatabaseConnectionRatio': max(b['databaseConnections'] / b['databaseConnectionLimit'] for b in buckets),
        'maximumFreshnessSeconds': freshness(run['edits'], len(buckets) * MINUTE_SECONDS),
        'importP95ChangeMs': import_impact(run),
        'firstFiveMinutesMaximumP95Ms': max(b['p95LatencyMs'] for b in buckets[1:6]),
        'firstFiveMinutesErrorRate': sum(b['errors'] for b in buckets[1:6]) /
        sum(sum(b['publicRequests'].values()) for b in buckets[1:6]),
        'latencyTimeline': [{'offsetSeconds': b['offsetSeconds'], 'p95LatencyMs': b['p95LatencyMs']}
                            for b in buckets],
    }


def evaluate(source, policy):
    report = {'analysisPassed': False, 'releaseQualified': False, 'failures': [], 'runs': {}}
    try:
        require(source['schema'] == 'honua.surge-observations/v1', 'unknown observation schema')
        require(re.fullmatch(r'[a-f0-9]{64}', source['candidateManifestSha256']) is not None,
                'candidate manifest digest required')
        host = urlparse(source['publicHost'])
        require(host.scheme == 'https' and host.hostname and not host.username and not host.password,
                'HTTPS public hostname required')
        require(source['directOriginRefused'] is True, 'direct origin must refuse access')
        _number(policy['maximumErrorRate'], high=1)
        _number(policy['maximumOriginGrowthFraction'], low=0, high=1)
        require(0 < policy['maximumOriginGrowthFraction'] < 1, 'origin growth fraction must be between zero and one')
        default = _count(source['defaultMinimumTasks'])
        require([r['name'] for r in source['runs']] == ['default', 'prescaled'], 'two ordered runs required')
        require(source['runs'][0]['minimumTasks'] == default, 'first run must use deployment defaults')
        require(source['runs'][1]['minimumTasks'] > default, 'second run must raise minimum tasks')
        for run in source['runs']:
            report['runs'][run['name']] = measure_run(run, policy, default)
        profiles = [workload(run, policy) for run in source['runs']]
        require(profiles[0] == profiles[1], 'comparison requires identical per-surface workload')
        before, after = (report['runs'][name] for name in ('default', 'prescaled'))
        report['firstFiveMinutesComparison'] = {
            'p95ChangeMs': after['firstFiveMinutesMaximumP95Ms'] - before['firstFiveMinutesMaximumP95Ms'],
            'errorRateChange': after['firstFiveMinutesErrorRate'] - before['firstFiveMinutesErrorRate'],
        }
        report['analysisPassed'] = True
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError) as exc:
        report['failures'].append(str(exc))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observations', required=True, type=Path)
    parser.add_argument('--policy', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        report = evaluate(json.loads(args.observations.read_text()), json.loads(args.policy.read_text()))
    except (OSError, ValueError) as exc:
        report = {'analysisPassed': False, 'releaseQualified': False, 'failures': [str(exc)]}
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + '\n')
    return 0 if report['analysisPassed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
