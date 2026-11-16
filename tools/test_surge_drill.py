"""Surge measurements must fail on missing coverage and unsafe serving."""
import copy

import pytest

from surge_drill import evaluate


def observations():
    runs = []
    for name, minimum in [('default', 2), ('prescaled', 4)]:
        buckets = []
        for minute in range(12):
            multiplier = 1 if minute == 0 else min(minute * 10, 100)
            buckets.append({
                'offsetSeconds': minute * 60, 'durationSeconds': 60,
                'publicRequests': {key: 100 * multiplier for key in ('map', 'tile', 'query')},
                'errors': 0, 'originRequests': 30, 'cacheHits': 270 * multiplier,
                'cacheRequests': 300 * multiplier, 'p95LatencyMs': 100,
                'tasks': minimum if minute == 0 else 6,
                'databaseConnections': 60, 'databaseConnectionLimit': 100,
                'taskConnectionFailures': 0,
            })
        runs.append({
            'name': name, 'minimumTasks': minimum, 'buckets': buckets,
            'scalingAlarmOffsetSeconds': 60, 'newTaskServingOffsetSeconds': 120,
            'edits': [{'committedOffsetSeconds': minute * 60, 'visibleOffsetSeconds': minute * 60 + 20,
                       'cacheLifetimeSeconds': 30, 'staleAfterLifetime': 0,
                       'afterLifetimeRequests': 5, 'lastProbeOffsetSeconds': minute * 60 + 40}
                      for minute in range(1, 12)],
            'privateProbes': [dict(offsetSeconds=minute * 60, durationSeconds=60, **{'ownerRequests': 10, 'otherUserRequests': 10,
                              'anonymousRequests': 10, 'ownerContentMissing': 0,
                              'crossUserLeaks': 0, 'anonymousLeaks': 0}) for minute in range(13)],
            'drain': {'startedOffsetSeconds': 720, 'endedOffsetSeconds': 780, 'finalTasks': 2,
                      'buckets': [{'offsetSeconds': 720, 'durationSeconds': 60, 'requests': 100, 'errors': 0}]},
            'import': {'startedOffsetSeconds': 120, 'endedOffsetSeconds': 240,
                       'result': 'succeeded'},
        })
    return {'schema': 'honua.surge-observations/v1', 'candidateManifestSha256': 'a' * 64,
            'publicHost': 'https://surge.example.test', 'directOriginRefused': True,
            'defaultMinimumTasks': 2, 'runs': runs}


POLICY = {'maximumErrorRate': 0.01, 'maximumOriginGrowthFraction': 0.1,
          'rampMultipliers': list(range(10, 101, 10))}


def test_reports_measured_comparison_without_release_credit():
    report = evaluate(observations(), POLICY)
    assert report['analysisPassed']
    assert report['releaseQualified'] is False
    assert report['runs']['default']['clientGrowth'] == 100
    assert report['runs']['default']['originGrowth'] == 1
    assert report['runs']['default']['scaleOutSeconds'] == 60
    assert report['runs']['default']['maximumFreshnessSeconds'] == 20
    assert report['runs']['default']['importP95ChangeMs'] == 0
    assert report['firstFiveMinutesComparison']['p95ChangeMs'] == 0


@pytest.mark.parametrize('mutate', [
    lambda x: x['runs'].pop(),
    lambda x: x['runs'][0]['buckets'].pop(4),
    lambda x: x['runs'][0]['buckets'][2].update(offsetSeconds=60),
    lambda x: x['runs'][0]['buckets'][10]['publicRequests'].update(tile=10),
    lambda x: x['runs'][0]['buckets'][10].update(originRequests=30000),
    lambda x: x['runs'][0]['buckets'][11]['publicRequests'].update(tile=100),
    lambda x: x['runs'][0]['buckets'][10]['publicRequests'].update(unmeasured=100000000),
    lambda x: x['runs'][0]['buckets'][0].update(offsetSeconds=False),
    lambda x: x['runs'][0]['buckets'][10].update(errors=400),
    lambda x: x['runs'][0]['buckets'][10].update(databaseConnections=100),
    lambda x: x['runs'][0]['buckets'][10].update(taskConnectionFailures=1),
    lambda x: x['runs'][0]['edits'].pop(4),
    lambda x: x['runs'][0]['edits'][4].update(visibleOffsetSeconds=400),
    lambda x: x['runs'][0]['edits'][4].update(staleAfterLifetime=1),
    lambda x: x['runs'][0]['edits'][4].update(afterLifetimeRequests=0),
    lambda x: x['runs'][0]['edits'][4].update(lastProbeOffsetSeconds=320),
    lambda x: x['runs'][0]['privateProbes'][0].update(otherUserRequests=0),
    lambda x: x['runs'][0]['privateProbes'][0].update(crossUserLeaks=1),
    lambda x: x['runs'][0]['privateProbes'][0].update(anonymousLeaks=1),
    lambda x: x['runs'][0]['drain']['buckets'][0].update(errors=1),
    lambda x: x['runs'][0]['drain'].update(finalTasks=6),
    lambda x: x['runs'][0]['import'].update(result='failed'),
    lambda x: x['runs'][0]['import'].update(startedOffsetSeconds=0),
    lambda x: x['runs'][1].update(minimumTasks=2),
    lambda x: x.update(directOriginRefused=False),
    lambda x: x.update(candidateManifestSha256='latest'),
    lambda x: x['runs'][0]['buckets'][0].update(p95LatencyMs=float('nan')),
    lambda x: x['runs'][0]['buckets'][0].update(errors=True),
])
def test_incomplete_or_unsafe_observations_fail(mutate):
    source = copy.deepcopy(observations())
    mutate(source)
    report = evaluate(source, POLICY)
    assert not report['analysisPassed']
    assert report['failures']
    assert report['releaseQualified'] is False


@pytest.mark.parametrize('policy', [{}, {'maximumErrorRate': float('nan')},
                                    dict(POLICY, maximumErrorRate=True),
                                    dict(POLICY, maximumOriginGrowthFraction=1)])
def test_requires_explicit_finite_pre_registered_thresholds(policy):
    assert not evaluate(observations(), policy)['analysisPassed']


def test_freshness_collection_can_outlast_editing_for_long_cache_lifetimes():
    source = observations()
    for run in source['runs']:
        last_bucket = dict(run['buckets'][-1], offsetSeconds=720)
        run['buckets'].append(last_bucket)
        run['drain'].update(startedOffsetSeconds=780, endedOffsetSeconds=840)
        run['drain']['buckets'][0]['offsetSeconds'] = 780
        run['privateProbes'].append(dict(run['privateProbes'][-1], offsetSeconds=780))
        for edit in run['edits']:
            edit.update(cacheLifetimeSeconds=90,
                        lastProbeOffsetSeconds=edit['committedOffsetSeconds'] + 100)
    assert evaluate(source, POLICY)['analysisPassed']


@pytest.mark.parametrize('valid_json', [False, True])
def test_cli_archives_result_and_uses_failure_exit_status(tmp_path, valid_json):
    import json
    from pathlib import Path
    import subprocess
    import sys

    source = tmp_path / 'observations.json'
    policy = tmp_path / 'policy.json'
    output = tmp_path / 'report.json'
    source.write_text(json.dumps(observations()) if valid_json else '{')
    policy.write_text(json.dumps(POLICY))
    result = subprocess.run([sys.executable, str(Path(__file__).with_name('surge_drill.py')),
                             '--observations', str(source), '--policy', str(policy),
                             '--output', str(output)], capture_output=True, text=True)
    assert result.returncode == (0 if valid_json else 1), result.stderr
    report = json.loads(output.read_text())
    assert report['analysisPassed'] is valid_json
    assert report['releaseQualified'] is False


def test_rejects_different_prescaled_workload():
    source = observations()
    for bucket in source['runs'][1]['buckets']:
        bucket['publicRequests'] = {key: value * 100 for key, value in bucket['publicRequests'].items()}
    assert not evaluate(source, POLICY)['analysisPassed']


def test_rejects_step_instead_of_ramp():
    source = observations()
    for run in source['runs']:
        for bucket in run['buckets'][1:10]:
            bucket.update(publicRequests={key: 100 for key in ('map', 'tile', 'query')},
                          cacheRequests=300, cacheHits=270)
    assert not evaluate(source, POLICY)['analysisPassed']


def test_rejects_serving_event_without_scaled_tasks():
    source = observations()
    source['runs'][0]['buckets'][2]['tasks'] = 2
    assert not evaluate(source, POLICY)['analysisPassed']


@pytest.mark.parametrize('field', ['privateProbes', 'drain'])
def test_rejects_probe_coverage_gap(field):
    source = observations()
    probes = source['runs'][0][field]
    if isinstance(probes, list):
        probes.pop(0)
    else:
        probes['buckets'].pop(0)
    assert not evaluate(source, POLICY)['analysisPassed']
