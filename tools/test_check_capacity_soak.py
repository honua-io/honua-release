import copy
import hashlib
import json
from pathlib import Path

import pytest
from datetime import datetime, timedelta, timezone

import check_capacity_soak as gate


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "certification" / "capacity-envelope.v1.json"
LOCK = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
REVISION = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
WINDOW = {"startedAt": "2026-09-06T10:06:00Z", "endedAt": "2026-09-06T11:06:00Z"}


def _sha(value: str | bytes) -> str:
    payload = value.encode() if isinstance(value, str) else value
    return hashlib.sha256(payload).hexdigest()


def receipt(artifact_root: Path):
    artifacts = {
        "requests": b'{"requests":180000,"errors":1}\n',
        "metrics": b'{"samples":3600}\n',
        "recovery": b'{"failure":"redis-restart","recovered":true}\n',
    }
    for name, payload in artifacts.items():
        (artifact_root / f"{name}.json").write_bytes(payload)

    raw_artifacts = [
        {
            "id": name,
            "kind": "request-ledger" if name == "requests" else "metric-export",
            "path": f"{name}.json",
            "uri": f"https://github.com/honua-io/honua-server/actions/runs/123456/artifacts/{100 + index}",
            "sha256": _sha(payload),
            "observationCount": 180000 if name == "requests" else 3600,
        }
        for index, (name, payload) in enumerate(artifacts.items())
    ]

    candidate = {"serverRevision": REVISION, "imageDigest": IMAGE_DIGEST}
    workloads = {
        name: {
            "status": "exercised",
            "target": target,
            "observed": target,
            "executionMode": "candidate-topology",
            "proxy": False,
            "rawArtifactIds": ["requests", "metrics"],
        }
        for name, target in LOCK["supportedEnvelope"].items()
    }

    values = {
        "availability": 179999 / 180000,
        "errorRate": 1 / 180000,
        "p95LatencyMs": 600,
        "p99LatencyMs": 630,
        "throughputRps": 1800,
        "queueAgeSeconds": 5,
        "saturationRatio": 0.7,
        "recoveryTimeSeconds": 120,
    }
    populations = {
        "availability": {"kind": "ratio", "numerator": 179999, "denominator": 180000, "sampleCount": 180000},
        "errorRate": {"kind": "ratio", "numerator": 1, "denominator": 180000, "sampleCount": 180000},
        "p95LatencyMs": {"kind": "distribution", "sampleCount": 180000},
        "p99LatencyMs": {"kind": "distribution", "sampleCount": 180000},
        "throughputRps": {"kind": "ratio", "numerator": 6480000, "denominator": 3600, "sampleCount": 6480000},
        "queueAgeSeconds": {"kind": "gauge", "sampleCount": 3600},
        "saturationRatio": {"kind": "gauge", "sampleCount": 10800},
        "recoveryTimeSeconds": {"kind": "duration", "sampleCount": 1},
    }

    signals = {}
    for name, value in values.items():
        expression = f"frozen_{name}_query"
        threshold = LOCK["thresholds"][name]
        signals[name] = {
            "status": "observed",
            "candidateIdentity": copy.deepcopy(candidate),
            "query": {
                "language": "promql",
                "expression": expression,
                "version": "2026.1",
                "sha256": _sha(expression),
            },
            "owner": "release-engineering",
            "alert": "https://github.com/honua-io/honua-release/blob/" + REVISION + "/docs/alerts/capacity.md",
            "runbook": "https://github.com/honua-io/honua-release/blob/" + REVISION + "/docs/runbooks/capacity.md",
            "window": copy.deepcopy(WINDOW),
            "observationPopulation": populations[name],
            "rawArtifactIds": ["requests", "metrics"],
            "workloadDimensions": list(workloads),
            "value": value,
            "thresholdVerdict": {
                "operator": threshold["operator"],
                "limit": threshold["value"],
                "passed": True,
            },
        }

    signals["saturationRatio"]["saturationComponents"] = {
        "worker": {"value": 0.6, "sampleCount": 3600, "rawArtifactIds": ["metrics"]},
        "database": {"value": 0.7, "sampleCount": 3600, "rawArtifactIds": ["metrics"]},
        "redis": {"value": 0.5, "sampleCount": 3600, "rawArtifactIds": ["metrics"]},
    }
    signals["recoveryTimeSeconds"]["recoveryEvidence"] = {
        "failure": "redis-restart",
        "injectedAt": "2026-09-06T10:30:00Z",
        "detectedAt": "2026-09-06T10:30:05Z",
        "recoveredAt": "2026-09-06T10:32:00Z",
        "rawArtifactIds": ["recovery"],
    }

    result = {
        "schemaVersion": 2,
        "status": "completed",
        "candidateIdentity": candidate,
        "observedRevision": REVISION,
        "lockSha256": hashlib.sha256(LOCK_PATH.read_bytes()).hexdigest(),
        "profile": "soak",
        "evidenceScope": "single-tenant-ga",
        "steadyStateSeconds": 3600,
        "window": copy.deepcopy(WINDOW),
        "envelope": copy.deepcopy(LOCK["supportedEnvelope"]),
        "topology": {
            "replicas": [
                {"id": "honua-a", "failureDomain": "zone-a", "imageDigest": IMAGE_DIGEST},
                {"id": "honua-b", "failureDomain": "zone-b", "imageDigest": IMAGE_DIGEST},
            ],
            "database": {"kind": "postgres", "failureDomain": "zone-c"},
            "redis": {"kind": "redis", "failureDomain": "zone-c"},
            "gpWorkers": 1,
        },
        "producer": {
            "repository": "honua-io/honua-server",
            "workflowPath": ".github/workflows/load-soak-nightly.yml",
            "workflowRef": f"honua-io/honua-server/.github/workflows/load-soak-nightly.yml@{REVISION}",
            "sourceRevision": REVISION,
            "runId": 123456,
            "runAttempt": 1,
            "predicateType": "https://slsa.dev/provenance/v1",
        },
        "rawArtifacts": raw_artifacts,
        "workloads": workloads,
        "signals": signals,
    }
    # Synthetic collector input is a checker fixture, never candidate qualification.
    start = datetime.fromisoformat(WINDOW['startedAt'].replace('Z', '+00:00'))
    times = [(start + timedelta(seconds=i)).isoformat().replace('+00:00', 'Z') for i in range(0, 3601, 60)]
    source = {key: copy.deepcopy(result[key]) for key in ('candidateIdentity', 'window', 'topology', 'producer', 'lockSha256')}
    source.update({
        'schema': 'honua.capacity-observations/v1', 'samplingFailures': [],
        'populationMode': 'complete-disjoint-intervals', 'samplePeriodSeconds': 60,
        'requestCount': 6480000,
        'requests': [dict(replica=replica, incarnation=replica+'-1', **WINDOW,
                          buckets=[{'count': count, 'durationMs': latency, 'httpStatus': status, 'inBandError': False, 'protocol': 'FeatureServer'}
                                   for count, latency, status in [(3000000, 600, 200), (239999, 630, 200), (1, 630, 500)]])
                     for replica in ('honua-a', 'honua-b')],
        'metrics': [dict(at=at, worker=.6, database=.7, redis=.5, queueAgeSeconds=5) for at in times],
        'workloads': [dict(at=at, dimensions=copy.deepcopy(LOCK['supportedEnvelope']), executionMode='candidate-topology', proxy=False) for at in times],
        'recoveries': [dict(dependency=dependency, failure=dependency+'-restart', probe='authenticated-serving-query',
                            injectedAt='2026-09-06T10:30:00Z', detectedAt='2026-09-06T10:30:05Z', recoveredAt='2026-09-06T10:32:00Z')
                       for dependency in ('worker', 'database', 'redis')],
    })
    # 95% is in the 630ms bucket unless 95% of the full population is 600ms.
    source['requests'][0]['buckets'][0]['count'] = 3100000
    source['requests'][0]['buckets'][1]['count'] = 139999
    source['requests'][1]['buckets'][0]['count'] = 3100000
    source['requests'][1]['buckets'][1]['count'] = 139999
    payload = json.dumps(source, sort_keys=True).encode()
    (artifact_root / 'observations.json').write_bytes(payload)
    result['rawArtifacts'].append(dict(id='observations', kind='capacity-observations', path='observations.json',
        uri='https://github.com/honua-io/honua-server/actions/runs/123456/artifacts/104', sha256=_sha(payload), observationCount=6480000))
    for name, signal in signals.items():
        signal['query'] = copy.deepcopy(LOCK['queries'][name])
        signal['topology'] = copy.deepcopy(result['topology'])
        signal['rawArtifactIds'] = ['observations']
        if name in ('availability', 'errorRate'):
            numerator = 6480000-2 if name == 'availability' else 2
            signal['value'] = numerator/6480000
            signal['observationPopulation'] = dict(kind='ratio', numerator=numerator, denominator=6480000, sampleCount=6480000)
        elif 'Latency' in name:
            signal['observationPopulation']['sampleCount'] = 6480000
        elif name == 'queueAgeSeconds':
            signal['observationPopulation']['sampleCount'] = len(times)
        elif name == 'saturationRatio':
            signal['observationPopulation']['sampleCount'] = len(times)*3
        elif name == 'recoveryTimeSeconds':
            signal['observationPopulation']['sampleCount'] = 3
    for name, workload in workloads.items():
        workload["query"] = copy.deepcopy(LOCK["workloadQueries"][name])
        workload["observationPopulation"] = dict(kind="ratio", numerator=len(times), denominator=len(times), sampleCount=len(times))
        workload.update(candidateIdentity=copy.deepcopy(candidate), window=copy.deepcopy(WINDOW), sampleCount=len(times), rawArtifactIds=['observations'])
    for component in signals['saturationRatio']['saturationComponents'].values():
        component.update(sampleCount=len(times), rawArtifactIds=['observations'])
    signals['recoveryTimeSeconds']['recoveryEvidence'] = {'events': source['recoveries'], 'rawArtifactIds': ['observations']}
    return result


def failures(value, artifact_root: Path):
    return gate.evaluate(LOCK, value, gate.lock_digest(LOCK_PATH), REVISION, artifact_root, IMAGE_DIGEST)


def test_complete_candidate_bound_evidence_bundle_passes(tmp_path):
    assert failures(receipt(tmp_path), tmp_path) == []


def test_hashed_bare_assertions_are_not_raw_observations(tmp_path):
    value = receipt(tmp_path)
    for artifact in value["rawArtifacts"]:
        payload = b'{"looksOfficial":true}\n'
        (tmp_path / artifact["path"]).write_bytes(payload)
        artifact["sha256"] = _sha(payload)
    assert any("source observations" in failure for failure in failures(value, tmp_path))


def test_rehashing_a_post_result_query_does_not_change_the_frozen_query(tmp_path):
    value = receipt(tmp_path)
    query = value["signals"]["availability"]["query"]
    query["expression"] = "return 1.0"
    query["sha256"] = _sha(query["expression"])
    assert any("frozen query" in failure for failure in failures(value, tmp_path))


def test_bare_numeric_signal_fails(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["availability"] = 1.0
    assert any("availability: missing" in failure for failure in failures(value, tmp_path))


def test_missing_or_changed_query_fails(tmp_path):
    missing = receipt(tmp_path)
    missing["signals"]["p95LatencyMs"].pop("query")
    assert any("p95LatencyMs: query" in failure for failure in failures(missing, tmp_path))

    changed = receipt(tmp_path)
    changed["signals"]["p95LatencyMs"]["query"]["expression"] = "post_result_query"
    assert any("p95LatencyMs: query hash" in failure for failure in failures(changed, tmp_path))


def test_missing_population_or_ratio_denominator_fails(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["availability"]["observationPopulation"].pop("denominator")
    assert any("availability: ratio" in failure for failure in failures(value, tmp_path))


def test_unexercised_or_preview_proxy_workload_fails(tmp_path):
    unexercised = receipt(tmp_path)
    unexercised["workloads"]["gpQueueDepth"]["status"] = "declared"
    assert any("gpQueueDepth: workload was not exercised" in failure for failure in failures(unexercised, tmp_path))

    proxy = receipt(tmp_path)
    proxy["workloads"]["gpQueueDepth"]["proxy"] = True
    proxy["workloads"]["gpQueueDepth"]["executionMode"] = "preview-proxy"
    assert any("gpQueueDepth: Preview/proxy" in failure for failure in failures(proxy, tmp_path))


def test_missing_workload_dimension_fails(tmp_path):
    value = receipt(tmp_path)
    value["workloads"].pop("services")
    assert any("services: workload" in failure for failure in failures(value, tmp_path))


def test_missing_or_tampered_raw_artifact_fails(tmp_path):
    value = receipt(tmp_path)
    (tmp_path / "requests.json").write_text("tampered", encoding="utf-8")
    assert any("requests: raw artifact hash mismatch" in failure for failure in failures(value, tmp_path))


def test_signal_must_reference_raw_artifact_and_workload(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["queueAgeSeconds"]["rawArtifactIds"] = []
    value["signals"]["queueAgeSeconds"]["workloadDimensions"] = []
    result = failures(value, tmp_path)
    assert any("queueAgeSeconds: raw artifact" in failure for failure in result)
    assert any("queueAgeSeconds: workload" in failure for failure in result)


def test_recovery_requires_exercised_timeline_and_artifact(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["recoveryTimeSeconds"].pop("recoveryEvidence")
    assert any("recoveryTimeSeconds: recovery evidence" in failure for failure in failures(value, tmp_path))


def test_saturation_requires_worker_database_and_redis_observations(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["saturationRatio"]["saturationComponents"].pop("redis")
    assert any("saturationRatio: redis" in failure for failure in failures(value, tmp_path))


def test_wrong_candidate_image_or_source_built_identity_fails(tmp_path):
    value = receipt(tmp_path)
    value["candidateIdentity"]["imageDigest"] = "source-build"
    assert any("candidate image digest" in failure for failure in failures(value, tmp_path))


def test_signal_candidate_identity_must_match_receipt(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["errorRate"]["candidateIdentity"]["imageDigest"] = "sha256:" + "c" * 64
    assert any("errorRate: candidate identity" in failure for failure in failures(value, tmp_path))


def test_only_approved_producer_is_accepted(tmp_path):
    value = receipt(tmp_path)
    value["producer"]["workflowPath"] = ".github/workflows/anything.yml"
    assert any("approved soak producer" in failure for failure in failures(value, tmp_path))


def test_threshold_verdict_cannot_disagree_with_value(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["throughputRps"]["thresholdVerdict"]["passed"] = False
    assert any("throughputRps: threshold verdict" in failure for failure in failures(value, tmp_path))


def test_per_tenant_or_demo_sla_scope_fails(tmp_path):
    per_tenant = receipt(tmp_path)
    per_tenant["tenantSlo"] = {"tenantId": "demo", "availability": 1.0}
    assert any("per-tenant SLO" in failure for failure in failures(per_tenant, tmp_path))

    demo = receipt(tmp_path)
    demo["evidenceScope"] = "demo-environment-sla"
    assert any("single-tenant GA" in failure for failure in failures(demo, tmp_path))


def test_skipped_signal_and_lock_drift_fail(tmp_path):
    value = receipt(tmp_path)
    value["signals"]["p99LatencyMs"]["status"] = "skipped"
    value["lockSha256"] = "0" * 64
    result = failures(value, tmp_path)
    assert any("p99LatencyMs" in failure for failure in result)
    assert any("exact committed" in failure for failure in result)


def rewrite_source(value, root, mutate):
    artifact = next(a for a in value['rawArtifacts'] if a['id'] == 'observations')
    path = root / artifact['path']
    source = json.loads(path.read_text())
    mutate(source)
    payload = json.dumps(source).encode()
    path.write_bytes(payload)
    artifact['sha256'] = _sha(payload)


@pytest.mark.parametrize('mutation', [
    lambda s: s['requests'][0]['buckets'][0].update(count=1),
    lambda s: s['requests'][0]['buckets'][0].update(inBandError=True),
    lambda s: s['requests'][0].update(endedAt='2026-09-06T10:36:00Z'),
    lambda s: s['requests'].append(copy.deepcopy(s['requests'][0])),
    lambda s: s.update(samplingFailures=['metrics timeout']),
    lambda s: s['metrics'].pop(10),
    lambda s: s['workloads'][0]['dimensions'].update(gpQueueDepth=0),
    lambda s: s['workloads'][0].update(proxy=True),
    lambda s: s['recoveries'].pop(),
    lambda s: s['recoveries'][0].update(recoveredAt='2026-09-06T10:30:00Z'),
    lambda s: s['candidateIdentity'].update(imageDigest='sha256:'+'c'*64),
    lambda s: s['metrics'][0].update(redis=.9),
])
def test_changed_raw_observations_fail_even_with_updated_hash(tmp_path, mutation):
    value = receipt(tmp_path)
    rewrite_source(value, tmp_path, mutation)
    assert any('source observations' in item for item in failures(value, tmp_path))


def test_digest_shaped_wrong_candidate_is_rejected(tmp_path):
    value = receipt(tmp_path)
    assert any('manifest-pinned image' in item for item in gate.evaluate(LOCK, value, gate.lock_digest(LOCK_PATH), REVISION, tmp_path, 'sha256:'+'c'*64))


def test_preview_dimensions_cannot_be_reintroduced_as_ga_workloads(tmp_path):
    value = receipt(tmp_path)
    assert set(LOCK['excludedPreviewDimensions']) == {'activeSubscriptions', 'alertEvaluationsPerSecond'}
    assert len(LOCK['supportedEnvelope']) == 8
    value['workloads']['activeSubscriptions'] = copy.deepcopy(value['workloads']['services'])
    assert any('outside the frozen envelope' in item for item in failures(value, tmp_path))


@pytest.mark.parametrize('dimension', list(LOCK['supportedEnvelope']))
def test_each_ga_workload_requires_frozen_query_and_population(tmp_path, dimension):
    value = receipt(tmp_path)
    value['workloads'][dimension].pop('query')
    assert any(dimension+': workload query' in item for item in failures(value, tmp_path))
    value = receipt(tmp_path)
    value['workloads'][dimension]['observationPopulation']['denominator'] -= 1
    assert any('source observations' in item for item in failures(value, tmp_path))


@pytest.mark.parametrize('signal', LOCK['soak']['requiredSignals'])
def test_each_signal_value_and_population_must_be_recomputed(tmp_path, signal):
    value = receipt(tmp_path)
    value['signals'][signal]['value'] += .00001
    assert any('source observations' in item for item in failures(value, tmp_path))
    value = receipt(tmp_path)
    value['signals'][signal]['observationPopulation']['sampleCount'] += 1
    assert any('source observations' in item for item in failures(value, tmp_path))


def test_raw_artifact_cannot_be_borrowed_from_another_run(tmp_path):
    value = receipt(tmp_path)
    value['rawArtifacts'][0]['uri'] = 'https://github.com/honua-io/honua-server/actions/runs/999999/artifacts/100'
    assert any('different producer run' in item for item in failures(value, tmp_path))
