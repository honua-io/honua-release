#!/usr/bin/env python3
"""Fail-closed validation and receipt emission for RC2 runtime and security rigor gates."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import math
from pathlib import Path
import re

DIGEST_RE = re.compile(r'^sha256:[0-9a-f]{64}$')
SHA_RE = re.compile(r'^[0-9a-f]{40}$')

AOT_ARCHES = frozenset({'amd64', 'arm64'})
AOT_REQUIRED_FLAGS = (
    'healthyReady', 'geoservicesJsonPbf', 'mapExportSkia', 'parquetNative',
    'ogcWeakEtag304', 'odataConditional', 'wmsGetMap', 'oidcOauthCallback',
)
UPGRADE_REQUIRED_FLAGS = (
    'degradedStartupSurvived', 'degradedInitialNotReady', 'editableAfterUpgrade',
)
REQUIRED_CLIENTS = frozenset({'qgis', 'maplibre', 'cesium', 'geoservices'})
REQUIRED_CORS_ERROR_CODES = frozenset({400, 401, 402, 403, 429})
CLIENT_PROXY_FLAGS = (
    'forwardedProtoHttps', 'ogcNextLinksHttps', 'capabilitiesOnlineResourceHttps',
    'axisOrderVerified', 'geoservicesErrorEnvelopeVerified',
)
REQUIRED_HOST_ADMIN_ROUTES = frozenset({
    '/api/v1/admin/connections',
    '/api/v1/admin/roles',
    '/api/v1/admin/rls-policies',
    '/api/v1/admin/field-mask-policies',
    '/api/v1/console/access/*',
    '/api/v1/admin/operations/streaming',
    '/api/v1/admin/observability/*',
    '/api/v1/admin/cache/*',
})
REQUIRED_HIDDEN_PROTOCOLS = frozenset({
    'geoservices', 'ogc', 'odata', 'stac', 'spatial-analytics',
})
REQUIRED_SOAK_WORKLOADS = frozenset({
    'featureserver-pbf', 'featureserver-json', 'ogc-items', 'mvt-tiles', 'skia-export',
})
MIN_SOAK_SECONDS = 600
MIN_MEMORY_LIMIT_BYTES = 134_217_728
MAX_MEMORY_LIMIT_BYTES = 4_294_967_296
MAX_MEMORY_GROWTH_RATIO = 1.25
MIN_SOAK_REQUESTS = 1000
MIN_SINGLE_TENANT_WORKFLOWS = 7

IDS = frozenset({
    'aot-multiarch-regression',
    'postgis-upgrade-recovery',
    'gis-clients-proxy',
    'tenant-isolation-parity',
    'aot-memory-soak',
})


@dataclass(frozen=True)
class ReceiptContext:
    row: str
    server_sha: str
    image_digest: str
    lock_digest: str


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_positive_finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _all_true(mapping: dict, keys: tuple[str, ...]) -> bool:
    return all(mapping.get(key) is True for key in keys)


def _valid_aot_arch(entry: object) -> str | None:
    if not isinstance(entry, dict) or not _all_true(entry, AOT_REQUIRED_FLAGS):
        return None
    digest = entry.get('platformDigest')
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest):
        return None
    if not _is_int(entry.get('unhandledExceptions')) or entry['unhandledExceptions'] != 0:
        return None
    return digest


def _valid_aot_multiarch(doc: dict) -> bool:
    arches = doc.get('architectures')
    if not isinstance(arches, dict) or set(arches) != AOT_ARCHES:
        return False
    digests = [_valid_aot_arch(arches[arch]) for arch in AOT_ARCHES]
    return all(digests) and len(set(digests)) == len(AOT_ARCHES)


def _valid_postgis_upgrade(doc: dict) -> bool:
    prior = doc.get('priorRcImageDigest')
    if not isinstance(prior, str) or not DIGEST_RE.fullmatch(prior):
        return False
    if prior == doc.get('imageDigest'):
        return False
    pre = doc.get('preUpgradeFeatureCount')
    post = doc.get('postUpgradeFeatureCount')
    if not (_is_int(pre) and _is_int(post) and pre > 0 and post >= pre):
        return False
    if not (_is_int(doc.get('migrationLockFailures')) and doc['migrationLockFailures'] == 0):
        return False
    if not (_is_int(doc.get('recoveredReadyHttpStatus')) and doc['recoveredReadyHttpStatus'] == 200):
        return False
    return _all_true(doc, UPGRADE_REQUIRED_FLAGS)


def _valid_client_entry(entry: object) -> bool:
    return (isinstance(entry, dict) and entry.get('status') == 'PASS'
            and isinstance(entry.get('evidence'), str) and bool(entry['evidence'].strip()))


def _valid_gis_clients_proxy(doc: dict) -> bool:
    clients = doc.get('clients')
    if not isinstance(clients, dict) or not REQUIRED_CLIENTS <= set(clients):
        return False
    if not all(_valid_client_entry(clients[name]) for name in REQUIRED_CLIENTS):
        return False
    cors_codes = doc.get('corsOnErrorCodes')
    if not isinstance(cors_codes, list) or any(not _is_int(code) for code in cors_codes):
        return False
    if not REQUIRED_CORS_ERROR_CODES <= set(cors_codes):
        return False
    return _all_true(doc, CLIENT_PROXY_FLAGS)


def _valid_tenant_isolation(doc: dict) -> bool:
    if doc.get('singleTenantConsoleZero403') is not True or doc.get('platformAdminSucceeded') is not True:
        return False
    workflows = doc.get('singleTenantWorkflowsPassed')
    if not _is_int(workflows) or workflows < MIN_SINGLE_TENANT_WORKFLOWS:
        return False
    if not (_is_int(doc.get('tenantAdminAllowedHostRoutes')) and doc['tenantAdminAllowedHostRoutes'] == 0):
        return False
    if not (_is_int(doc.get('crossTenantLeaks')) and doc['crossTenantLeaks'] == 0):
        return False
    routes = doc.get('multiTenantPlatformAdminRequiredRoutes')
    protocols = doc.get('hiddenFieldProtocolsBlocked')
    if not isinstance(routes, list) or not isinstance(protocols, list):
        return False
    return (REQUIRED_HOST_ADMIN_ROUTES <= set(routes)
            and REQUIRED_HIDDEN_PROTOCOLS <= set(protocols))


def _valid_soak_memory(doc: dict) -> bool:
    limit = doc.get('memoryLimitBytes')
    peak = doc.get('peakRssBytes')
    warmup = doc.get('warmupRssBytes')
    final = doc.get('finalRssBytes')
    if not all(_is_int(value) for value in (limit, peak, warmup, final)):
        return False
    if not MIN_MEMORY_LIMIT_BYTES <= limit <= MAX_MEMORY_LIMIT_BYTES:
        return False
    if not (0 < warmup <= peak < limit and 0 < final <= peak):
        return False
    return final <= warmup * MAX_MEMORY_GROWTH_RATIO


def _valid_soak_latency(doc: dict) -> bool:
    p50, p95, p99 = doc.get('p50LatencyMs'), doc.get('p95LatencyMs'), doc.get('p99LatencyMs')
    return (all(_is_positive_finite(value) for value in (p50, p95, p99))
            and p50 <= p95 <= p99)


def _valid_aot_memory_soak(doc: dict) -> bool:
    duration = doc.get('durationSeconds')
    if not _is_positive_finite(duration) or duration < MIN_SOAK_SECONDS:
        return False
    if doc.get('swapDisabled') is not True:
        return False
    if not (_is_int(doc.get('oomKillCount')) and doc['oomKillCount'] == 0):
        return False
    if not (_is_int(doc.get('http5xxCount')) and doc['http5xxCount'] == 0):
        return False
    total = doc.get('totalRequests')
    if not _is_int(total) or total < MIN_SOAK_REQUESTS:
        return False
    workloads = doc.get('workloadsCovered')
    if not isinstance(workloads, list) or not REQUIRED_SOAK_WORKLOADS <= set(workloads):
        return False
    return _valid_soak_memory(doc) and _valid_soak_latency(doc)


VALIDATORS = {
    'aot-multiarch-regression': _valid_aot_multiarch,
    'postgis-upgrade-recovery': _valid_postgis_upgrade,
    'gis-clients-proxy': _valid_gis_clients_proxy,
    'tenant-isolation-parity': _valid_tenant_isolation,
    'aot-memory-soak': _valid_aot_memory_soak,
}


def valid(doc: dict) -> bool:
    validator = VALIDATORS.get(doc.get('row', ''))
    return validator is not None and validator(doc)


def build_receipt(context: ReceiptContext, proof: dict) -> dict:
    if context.row not in IDS:
        raise ValueError(f'unsupported rigor row: {context.row}')
    if not SHA_RE.fullmatch(context.server_sha):
        raise ValueError('invalid server_sha')
    if not DIGEST_RE.fullmatch(context.image_digest) or not DIGEST_RE.fullmatch(context.lock_digest):
        raise ValueError('invalid candidate digests')
    if not isinstance(proof, dict):
        raise ValueError('proof document must be a JSON object')
    receipt = {
        **proof,
        'schema': 'honua.release-promise-receipt/v1',
        'row': context.row,
        'status': 'PASS',
        'serverSha': context.server_sha,
        'imageDigest': context.image_digest,
        'platformLockDigest': context.lock_digest,
        'observedAt': dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z'),
        'assertions': {'live': True, 'rigorVerified': True},
    }
    if not valid(receipt):
        raise ValueError(f'proof failed fail-closed validation for {context.row}')
    return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--row', choices=sorted(IDS), required=True)
    parser.add_argument('--proof', type=Path, required=True)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--lock-digest', required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.proof.is_file():
        parser.exit(1, f'Missing rigor proof file: {args.proof}\n')
    try:
        proof = json.loads(args.proof.read_text(encoding='utf-8'))
        context = ReceiptContext(args.row, args.server_sha, args.server_digest, args.lock_digest)
        receipt = build_receipt(context, proof)
    except (OSError, ValueError) as error:
        parser.exit(1, f'RC2 rigor gate rejected: {error}\n')
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f'{args.row}.json').write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
