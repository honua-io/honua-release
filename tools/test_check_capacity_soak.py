import copy
import hashlib
import json
from pathlib import Path

import check_capacity_soak as gate


ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / "certification" / "capacity-envelope.v1.json"
LOCK = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
REVISION = "a" * 40
IMAGE_DIGEST = "sha256:" + "b" * 64
WINDOW = {"startedAt": "2026-09-01T10:06:00Z", "endedAt": "2026-09-01T11:06:00Z"}


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
        "injectedAt": "2026-09-01T10:30:00Z",
        "detectedAt": "2026-09-01T10:30:05Z",
        "recoveredAt": "2026-09-01T10:32:00Z",
        "rawArtifactIds": ["recovery"],
    }

    return {
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


def failures(value, artifact_root: Path):
    return gate.evaluate(LOCK, value, gate.lock_digest(LOCK_PATH), REVISION, artifact_root)


def test_complete_candidate_bound_evidence_bundle_passes(tmp_path):
    assert failures(receipt(tmp_path), tmp_path) == []


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
    proxy["workloads"]["activeSubscriptions"]["proxy"] = True
    proxy["workloads"]["activeSubscriptions"]["executionMode"] = "preview-proxy"
    assert any("activeSubscriptions: Preview/proxy" in failure for failure in failures(proxy, tmp_path))


def test_missing_workload_dimension_fails(tmp_path):
    value = receipt(tmp_path)
    value["workloads"].pop("alertEvaluationsPerSecond")
    assert any("alertEvaluationsPerSecond: workload" in failure for failure in failures(value, tmp_path))


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
