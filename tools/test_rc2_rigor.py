"""Fail-closed positive and adversarial tests for RC2 rigor gates."""
from __future__ import annotations

import datetime as dt

import pytest

import rc2_rigor as rigor
import rc_gate_window as w
import release_promise as promise

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64
LOCK = 'sha256:' + 'c' * 64
NOW = dt.datetime(2026, 11, 24, 18, 0, tzinfo=dt.timezone.utc)
OBSERVED_AT = '2026-11-24T17:00:00Z'
CANDIDATE = w.Candidate(SHA, DIGEST, LOCK)


def _identity(observed_at: str = OBSERVED_AT) -> dict:
    return {
        'serverSha': SHA,
        'imageDigest': DIGEST,
        'platformLockDigest': LOCK,
        'observedAt': observed_at,
    }


def valid_aot_proof() -> dict:
    base = {key: True for key in rigor.AOT_REQUIRED_FLAGS}
    return {
        **_identity(),
        'architectures': {
            'amd64': {**base, 'platformDigest': 'sha256:' + '1' * 64, 'unhandledExceptions': 0},
            'arm64': {**base, 'platformDigest': 'sha256:' + '2' * 64, 'unhandledExceptions': 0},
        },
    }


def valid_upgrade_proof() -> dict:
    return {
        **_identity(),
        'priorRcImageDigest': 'sha256:' + '9' * 64,
        'preUpgradeFeatureCount': 120,
        'postUpgradeFeatureCount': 125,
        'migrationLockFailures': 0,
        'recoveredReadyHttpStatus': 200,
        'degradedStartupSurvived': True,
        'degradedInitialNotReady': True,
        'editableAfterUpgrade': True,
    }


def valid_clients_proof() -> dict:
    return {
        **_identity(),
        'clients': {
            name: {'status': 'PASS', 'evidence': f'artifacts/{name}-smoke.log'}
            for name in rigor.REQUIRED_CLIENTS
        },
        'corsOnErrorCodes': [400, 401, 402, 403, 429],
        **{flag: True for flag in rigor.CLIENT_PROXY_FLAGS},
    }


def valid_tenant_proof() -> dict:
    return {
        **_identity(),
        'singleTenantConsoleZero403': True,
        'singleTenantWorkflowsPassed': 7,
        'platformAdminSucceeded': True,
        'tenantAdminAllowedHostRoutes': 0,
        'crossTenantLeaks': 0,
        'multiTenantPlatformAdminRequiredRoutes': sorted(rigor.REQUIRED_HOST_ADMIN_ROUTES),
        'hiddenFieldProtocolsBlocked': sorted(rigor.REQUIRED_HIDDEN_PROTOCOLS),
    }


def valid_soak_proof() -> dict:
    return {
        **_identity(),
        'durationSeconds': 600,
        'memoryLimitBytes': 1_073_741_824,
        'swapDisabled': True,
        'warmupRssBytes': 180_000_000,
        'peakRssBytes': 240_000_000,
        'finalRssBytes': 210_000_000,
        'oomKillCount': 0,
        'http5xxCount': 0,
        'totalRequests': 25_000,
        'workloadsCovered': sorted(rigor.REQUIRED_SOAK_WORKLOADS),
        'p50LatencyMs': 12.4,
        'p95LatencyMs': 44.1,
        'p99LatencyMs': 88.9,
    }


PROOFS = {
    'aot-multiarch-regression': valid_aot_proof,
    'postgis-upgrade-recovery': valid_upgrade_proof,
    'gis-clients-proxy': valid_clients_proof,
    'tenant-isolation-parity': valid_tenant_proof,
    'aot-memory-soak': valid_soak_proof,
}


def test_every_rc_required_workflow_exists_on_disk():
    assert promise.missing_rc_workflows() == []


@pytest.mark.parametrize('row_id', sorted(rigor.IDS))
def test_rigor_valid_receipt_passes_both_emitter_and_promise_gate(row_id):
    context = rigor.ReceiptContext(row_id, SHA, DIGEST, LOCK)
    emit_now = dt.datetime(2026, 11, 24, 17, 2, tzinfo=dt.timezone.utc)
    receipt = rigor.build_receipt(context, PROOFS[row_id](), now=emit_now)
    assert receipt['sourceObservedAt'] == OBSERVED_AT
    row = next(r for r in promise.load_contract() if r['id'] == row_id)
    run = {
        'id': 10, 'status': 'completed', 'conclusion': 'success', 'head_branch': 'trunk',
        'event': 'workflow_dispatch', 'path': '.github/workflows/' + row['workflow'],
        'run_started_at': '2026-11-24T16:55:00Z', 'updated_at': '2026-11-24T17:05:00Z',
        'reports': [receipt],
    }
    assert promise.evaluate_row(row, [run], (CANDIDATE, NOW))['status'] == 'PASS'


@pytest.mark.parametrize('mutation', [
    {'serverSha': 'f' * 40},
    {'imageDigest': 'sha256:' + 'f' * 64},
    {'platformLockDigest': 'sha256:' + 'f' * 64},
    {'observedAt': '2020-01-01T00:00:00Z'},
    {'observedAt': '2026-11-24T18:05:00Z'},
    {'observedAt': ''},
])
def test_rigor_rejects_mismatched_identity_or_stale_timestamp(mutation):
    ctx = rigor.ReceiptContext('aot-multiarch-regression', SHA, DIGEST, LOCK)
    proof = {**valid_aot_proof(), **mutation}
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, proof, now=NOW)


def test_aot_rejects_single_arch_duplicate_digest_or_exception():
    ctx = rigor.ReceiptContext('aot-multiarch-regression', SHA, DIGEST, LOCK)
    missing_arm = valid_aot_proof()
    missing_arm['architectures'].pop('arm64')
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, missing_arm, now=NOW)

    dup = valid_aot_proof()
    dup['architectures']['arm64']['platformDigest'] = dup['architectures']['amd64']['platformDigest']
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, dup, now=NOW)

    exc = valid_aot_proof()
    exc['architectures']['amd64']['unhandledExceptions'] = 1
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, exc, now=NOW)


def test_upgrade_rejects_same_image_or_data_loss_or_lock_failure():
    ctx = rigor.ReceiptContext('postgis-upgrade-recovery', SHA, DIGEST, LOCK)
    for bad in (
        {'priorRcImageDigest': DIGEST},
        {'preUpgradeFeatureCount': 0},
        {'postUpgradeFeatureCount': 119},
        {'migrationLockFailures': 1},
        {'recoveredReadyHttpStatus': 503},
        {'degradedStartupSurvived': False},
    ):
        proof = {**valid_upgrade_proof(), **bad}
        with pytest.raises(ValueError):
            rigor.build_receipt(ctx, proof, now=NOW)


def test_clients_proxy_rejects_missing_client_or_cors_code():
    ctx = rigor.ReceiptContext('gis-clients-proxy', SHA, DIGEST, LOCK)
    missing_qgis = valid_clients_proof()
    missing_qgis['clients'].pop('qgis')
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, missing_qgis, now=NOW)

    missing_429 = valid_clients_proof()
    missing_429['corsOnErrorCodes'] = [400, 401, 402, 403]
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, missing_429, now=NOW)

    bad_flag = {**valid_clients_proof(), 'forwardedProtoHttps': False}
    with pytest.raises(ValueError):
        rigor.build_receipt(ctx, bad_flag, now=NOW)


def test_tenant_isolation_rejects_host_route_leak_or_missing_protocol():
    ctx = rigor.ReceiptContext('tenant-isolation-parity', SHA, DIGEST, LOCK)
    for bad in (
        {'tenantAdminAllowedHostRoutes': 1},
        {'crossTenantLeaks': 1},
        {'singleTenantConsoleZero403': False},
        {'singleTenantWorkflowsPassed': 6},
        {'hiddenFieldProtocolsBlocked': ['geoservices', 'ogc']},
        {'multiTenantPlatformAdminRequiredRoutes': ['/api/v1/admin/connections']},
    ):
        proof = {**valid_tenant_proof(), **bad}
        with pytest.raises(ValueError):
            rigor.build_receipt(ctx, proof, now=NOW)


def test_soak_rejects_short_run_oom_5xx_or_memory_leak():
    ctx = rigor.ReceiptContext('aot-memory-soak', SHA, DIGEST, LOCK)
    for bad in (
        {'durationSeconds': 599},
        {'swapDisabled': False},
        {'oomKillCount': 1},
        {'http5xxCount': 1},
        {'finalRssBytes': 240_000_000},
        {'peakRssBytes': 1_073_741_824},
        {'p95LatencyMs': 95.0},
        {'workloadsCovered': ['featureserver-pbf']},
    ):
        proof = {**valid_soak_proof(), **bad}
        with pytest.raises(ValueError):
            rigor.build_receipt(ctx, proof, now=NOW)
