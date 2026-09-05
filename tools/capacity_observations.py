"""Recompute capacity SLIs from attested collector observations, never receipt values.

Request observations are lossless joint histograms of duration and outcome, partitioned
by replica/incarnation and UTC bucket. Counts are disjoint interval deltas (not cumulative
counters or retained samples). Metric/workload samples and recovery events are retained.
Only the approved producer may attest this source format; this module is not a producer.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
from pathlib import Path


def _time(value):
    if not isinstance(value, str) or not value.endswith('Z'):
        raise ValueError('timestamps must be UTC with Z suffix')
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def _number(value, low=0, high=math.inf):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('non-finite or out-of-range observation')
    return value


def _count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError('observation count must be a positive integer')
    return value


def _equal(actual, expected, label):
    if actual != expected:
        raise ValueError(f'{label} differs from source observations')


def validate_sources(lock: dict, receipt: dict, root: Path) -> list[str]:
    """Verify sources, recompute all eight signals and compare every source population."""
    try:
        _validate(lock, receipt, root.resolve())
    except (KeyError, TypeError, ValueError, OSError, OverflowError) as exc:
        return [f'source observations: {exc}']
    return []


def _validate(lock, receipt, root):
    sources = [a for a in receipt['rawArtifacts'] if a['kind'] == 'capacity-observations']
    if len(sources) != 1:
        raise ValueError('exactly one capacity-observations artifact is required')
    artifact = sources[0]
    path = (root / artifact['path']).resolve()
    if path.parent != root:
        raise ValueError('source path escapes bundle')
    payload = path.read_bytes()
    _equal(hashlib.sha256(payload).hexdigest(), artifact['sha256'], 'source hash')
    source = json.loads(payload)
    _equal(source['schema'], 'honua.capacity-observations/v1', 'schema')
    for field in ('candidateIdentity', 'window', 'topology', 'producer', 'lockSha256'):
        _equal(source[field], receipt[field], field)
    _equal(source['samplingFailures'], [], 'sampling failures')
    _equal(source['populationMode'], 'complete-disjoint-intervals', 'population mode')
    start, end = (_time(receipt['window'][k]) for k in ('startedAt', 'endedAt'))
    duration = (end - start).total_seconds()
    if duration <= 0:
        raise ValueError('empty window')
    replicas = {r['id'] for r in receipt['topology']['replicas']}
    intervals = {r: [] for r in replicas}
    request_count = errors = 0
    histogram = []
    for interval in source['requests']:
        replica = interval['replica']
        if replica not in replicas or not isinstance(interval['incarnation'], str) or not interval['incarnation']:
            raise ValueError('unknown replica or missing incarnation')
        begin, finish = _time(interval['startedAt']), _time(interval['endedAt'])
        if not start <= begin < finish <= end:
            raise ValueError('request interval is outside the observation window')
        intervals[replica].append((begin, finish))
        for bucket in interval['buckets']:
            count = _count(bucket['count'])
            latency = _number(bucket['durationMs'])
            status = bucket['httpStatus']
            if isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599:
                raise ValueError('invalid HTTP outcome')
            if not isinstance(bucket['inBandError'], bool) or not bucket['protocol']:
                raise ValueError('protocol/in-band outcome missing')
            request_count += count
            errors += count if status >= 500 or bucket['inBandError'] else 0
            histogram.append((latency, count))
    for replica, spans in intervals.items():
        cursor = start
        for begin, finish in sorted(spans):
            if begin != cursor:
                raise ValueError(f'{replica}: gap or overlap in complete request population')
            cursor = finish
        if cursor != end:
            raise ValueError(f'{replica}: incomplete observation window')
    _count(request_count)
    _equal(source['requestCount'], request_count, 'request population')
    _equal(artifact['observationCount'], request_count, 'artifact population')
    histogram.sort()

    def percentile(q):
        rank = math.ceil(q * request_count)
        accumulated = 0
        for latency, count in histogram:
            accumulated += count
            if accumulated >= rank:
                return latency
        raise ValueError('empty distribution')

    # Periodic samples must cover the whole window; failed or absent sampling is fatal.
    period = _number(source['samplePeriodSeconds'], low=0.001, high=60)

    def samples(rows, label):
        if not rows:
            raise ValueError(f'{label}: absent samples')
        times = [_time(row['at']) for row in rows]
        if times != sorted(set(times)) or times[0] != start or times[-1] != end:
            raise ValueError(f'{label}: samples must cover the complete window in order')
        if any((b - a).total_seconds() > period for a, b in zip(times, times[1:])):
            raise ValueError(f'{label}: sampling gap')

    metrics = source['metrics']
    samples(metrics, 'metrics')
    components = {name: max(_number(row[name], high=1) for row in metrics)
                  for name in ('worker', 'database', 'redis')}
    queue_age = max(_number(row['queueAgeSeconds']) for row in metrics)
    workloads = source['workloads']
    samples(workloads, 'workloads')
    for row in workloads:
        _equal(set(row['dimensions']), set(lock['supportedEnvelope']), 'GA workload dimensions')
        _equal(row['executionMode'], 'candidate-topology', 'workload execution mode')
        _equal(row['proxy'], False, 'Preview proxy')
        for name, target in lock['supportedEnvelope'].items():
            _equal(_number(row['dimensions'][name]), target, f'{name} exercised target')
    for name in lock['supportedEnvelope']:
        workload = receipt['workloads'][name]
        _equal(workload['candidateIdentity'], receipt['candidateIdentity'], f'{name} candidate')
        _equal(workload['window'], receipt['window'], f'{name} window')
        _equal(workload['sampleCount'], len(workloads), f'{name} population')
        _equal(workload['observationPopulation'], {'kind': 'ratio', 'numerator': len(workloads), 'denominator': len(workloads), 'sampleCount': len(workloads)}, f'{name} observed workload population')
        if artifact['id'] not in workload['rawArtifactIds']:
            raise ValueError(f'{name} does not reference source observations')

    recovery_durations = []
    domains = set()
    for event in source['recoveries']:
        domains.add(event['dependency'])
        injected, detected, recovered = (_time(event[k]) for k in ('injectedAt', 'detectedAt', 'recoveredAt'))
        if not start <= injected <= detected < recovered <= end or not event['failure'] or not event['probe']:
            raise ValueError('recovery event lacks an ordered in-window timeline and recovery probe')
        recovery_durations.append((recovered - injected).total_seconds())
    _equal(domains, {'worker', 'database', 'redis'}, 'injected recovery dependencies')
    values = {
        'availability': (request_count-errors)/request_count,
        'errorRate': errors/request_count,
        'p95LatencyMs': percentile(.95), 'p99LatencyMs': percentile(.99),
        'throughputRps': request_count/duration,
        'queueAgeSeconds': queue_age, 'saturationRatio': max(components.values()),
        'recoveryTimeSeconds': max(recovery_durations)}
    for name, computed in values.items():
        signal = receipt['signals'][name]
        if artifact['id'] not in signal['rawArtifactIds']:
            raise ValueError(f'{name} does not reference source observations')
        _equal(signal['topology'], receipt['topology'], f'{name} topology')
        actual = _number(signal['value'])
        if not math.isclose(actual, computed, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f'{name} value differs from source observations')
        population = signal['observationPopulation']
        if name in ('availability', 'errorRate', 'throughputRps'):
            expected = {'kind': 'ratio', 'sampleCount': request_count,
                        'numerator': request_count-errors if name == 'availability' else errors if name == 'errorRate' else request_count,
                        'denominator': duration if name == 'throughputRps' else request_count}
        else:
            expected = {'kind': 'distribution' if 'Latency' in name else 'duration' if name == 'recoveryTimeSeconds' else 'gauge',
                        'sampleCount': request_count if 'Latency' in name else len(recovery_durations) if name == 'recoveryTimeSeconds' else len(metrics)*3 if name == 'saturationRatio' else len(metrics)}
        _equal(population, expected, f'{name} numerator/denominator/sample population')
    saturation = receipt['signals']['saturationRatio']['saturationComponents']
    for name, value in components.items():
        _equal(saturation[name]['value'], value, f'{name} saturation')
        _equal(saturation[name]['sampleCount'], len(metrics), f'{name} saturation population')
        if artifact['id'] not in saturation[name]['rawArtifactIds']:
            raise ValueError(f'{name} missing saturation source')
    _equal(receipt['signals']['recoveryTimeSeconds']['recoveryEvidence']['events'], source['recoveries'], 'recovery events')
