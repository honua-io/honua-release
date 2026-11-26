#!/usr/bin/env python3
"""Live Docker probe for RC2 PostGIS in-place upgrade and degraded startup recovery."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
import urllib.parse

import rc2_live_docker as docker_env
import rc2_live_probes_common as common
import rc2_rigor as rigor

ROW_ID = "postgis-upgrade-recovery"
SERVICE_NAME = "upgrade_probe"
DEGRADED_CHECK_WAIT_SECONDS = 3.0
LOCK_FAILURE_RE = re.compile(
    r"(migration lock|advisory lock|pg_advisory_lock|could not obtain lock|lock timeout)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class UpgradeProbeConfig:
    """Configuration for executing the live PostGIS upgrade and recovery probe."""

    candidate: common.CandidateSpec
    prior_rc_digest: str
    out_path: Path
    boot_timeout: float = common.DEFAULT_BOOT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class UpgradeObservation:
    """Raw live observations gathered during the prior-RC to candidate upgrade."""

    prior_rc_digest: str
    pre_count: int
    post_count: int
    migration_lock_failures: int
    recovered_ready_status: int
    degraded_startup_survived: bool
    degraded_initial_not_ready: bool
    editable_after_upgrade: bool


def count_lock_failures(logs: str) -> int:
    """Count migration advisory-lock failure lines in container logs."""
    return sum(
        1 for line in logs.splitlines()
        if LOCK_FAILURE_RE.search(line) and any(
            word in line.lower() for word in ("fail", "error", "timeout", "exception", "refus")
        )
    )


def query_feature_count(base_url: str, service_name: str, layer_id: int) -> int:
    """Query live FeatureServer feature count for the seeded layer."""
    query = urllib.parse.urlencode({"where": "1=1", "returnCountOnly": "true", "f": "json"})
    url = f"{base_url}/rest/services/{service_name}/FeatureServer/{layer_id}/query?{query}"
    reply, parsed = common.json_request(common.HttpSpec("GET", url))
    if reply.status != common.HTTP_OK or not isinstance(parsed, dict):
        raise RuntimeError(f"feature count query failed: HTTP {reply.status} {reply.body[:300]!r}")
    count = parsed.get("count")
    if type(count) is not int:
        raise RuntimeError(f"feature count response missing integer count: {parsed}")
    return count


def add_post_upgrade_feature(base_url: str, service_name: str, layer_id: int) -> bool:
    """Insert a new point feature through OGC API Features Part 4 after upgrade."""
    del service_name
    feature = {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [-157.82, 21.28]},
        "properties": {
            "name": "Waikiki Post-Upgrade Station",
            "category": "port",
            "secret_note": "post-upgrade-write-ok",
            "population": 19500,
        },
    }
    headers = {
        "Content-Type": "application/geo+json",
        "X-API-Key": common.ADMIN_PASSWORD,
    }
    url = f"{base_url}/ogc/features/collections/{layer_id}/items"
    reply = common.http_request(
        common.HttpSpec("POST", url, headers, json.dumps(feature).encode("utf-8"))
    )
    return reply.status in (common.HTTP_OK, common.HTTP_CREATED)


def _validate_prior_digest(config: UpgradeProbeConfig) -> str:
    prior = config.prior_rc_digest
    if not rigor.DIGEST_RE.fullmatch(prior):
        raise ValueError(f"invalid prior_rc_image_digest: {prior!r}")
    if prior in (config.candidate.server_digest, config.candidate.amd64_digest):
        raise ValueError("prior_rc_image_digest must differ from candidate digest")
    return f"{common.HONUA_IMAGE_REPO}@{prior}"


def _run_prior_phase(
    stack: docker_env.DockerStack, prior_image: str, timeout: float
) -> tuple[common.PublishedLayer, int]:
    docker_env.start_honua_container(docker_env.HonuaLaunchSpec(stack, prior_image))
    common.wait_http_status(f"{stack.base_url}/healthz/ready", common.HTTP_OK, timeout)
    docker_env.seed_probe_table(stack.pg_container)
    published = docker_env.publish_postgis_layer(
        docker_env.PublishSpec(stack.base_url, stack.pg_container, SERVICE_NAME, editable=True)
    )
    pre_count = query_feature_count(stack.base_url, published.service_name, published.layer_id)
    common.run_docker(["rm", "-f", stack.app_container])
    return published, pre_count


def _verify_degraded_startup(
    stack: docker_env.DockerStack, candidate_image: str, timeout: float
) -> tuple[bool, bool]:
    common.run_docker(["stop", stack.pg_container])
    degraded_env = (
        "HONUA_DB_DEGRADED_START=true",
        "Database__StartupResilience__DegradedStartEnabled=true",
        "Database__StartupResilience__RetryMigrationsInBackground=true",
        "Database__StartupResilience__RetryBaseDelay=00:00:01",
        "Database__StartupResilience__RetryMaxDelay=00:00:03",
        "Database__MigrationSafety__ContractApplyPolicy=Auto",
    )
    docker_env.start_honua_container(
        docker_env.HonuaLaunchSpec(stack, candidate_image, degraded_env)
    )
    common.wait_http_status(f"{stack.base_url}/healthz/live", common.HTTP_OK, timeout)
    time.sleep(DEGRADED_CHECK_WAIT_SECONDS)
    survived = docker_env.container_is_running(stack.app_container)
    ready_reply = common.http_request(common.HttpSpec("GET", f"{stack.base_url}/healthz/ready"))
    initial_not_ready = ready_reply.status != common.HTTP_OK and ready_reply.status != 0
    return survived, initial_not_ready


def _recover_and_verify_upgrade(
    stack: docker_env.DockerStack, published: common.PublishedLayer, timeout: float
) -> tuple[int, int, bool, int]:
    common.run_docker(["start", stack.pg_container])
    docker_env.wait_postgis(stack.pg_container, timeout)
    ready_status = common.wait_http_status(f"{stack.base_url}/healthz/ready", common.HTTP_OK, timeout)
    logs = docker_env.read_container_logs(stack.app_container)
    lock_failures = count_lock_failures(logs)
    editable = add_post_upgrade_feature(stack.base_url, published.service_name, published.layer_id)
    post_count = query_feature_count(stack.base_url, published.service_name, published.layer_id)
    return ready_status, lock_failures, editable, post_count


def build_upgrade_proof(
    spec: common.CandidateSpec, obs: UpgradeObservation, observed_at: str | None = None
) -> dict[str, object]:
    """Assemble the fail-closed postgis-upgrade-recovery proof JSON payload."""
    return {
        **common.build_identity_fields(spec, observed_at),
        "priorRcImageDigest": obs.prior_rc_digest,
        "preUpgradeFeatureCount": obs.pre_count,
        "postUpgradeFeatureCount": obs.post_count,
        "migrationLockFailures": obs.migration_lock_failures,
        "recoveredReadyHttpStatus": obs.recovered_ready_status,
        "degradedStartupSurvived": obs.degraded_startup_survived,
        "degradedInitialNotReady": obs.degraded_initial_not_ready,
        "editableAfterUpgrade": obs.editable_after_upgrade,
    }


def run_upgrade_probe(config: UpgradeProbeConfig) -> dict[str, object]:
    """Execute the live prior-RC -> candidate PostGIS upgrade and degraded startup probe."""
    candidate_image = common.verify_candidate_image(config.candidate)
    prior_image = _validate_prior_digest(config)
    stack = docker_env.create_network_and_postgis(
        "rc2-upgrade", config.candidate.postgis_image, config.boot_timeout
    )
    try:
        published, pre_count = _run_prior_phase(stack, prior_image, config.boot_timeout)
        survived, initial_not_ready = _verify_degraded_startup(
            stack, candidate_image, config.boot_timeout
        )
        ready_status, lock_failures, editable, post_count = _recover_and_verify_upgrade(
            stack, published, config.boot_timeout
        )
    finally:
        docker_env.cleanup_stack(stack)
    obs = UpgradeObservation(
        prior_rc_digest=config.prior_rc_digest,
        pre_count=pre_count,
        post_count=post_count,
        migration_lock_failures=lock_failures,
        recovered_ready_status=ready_status,
        degraded_startup_survived=survived,
        degraded_initial_not_ready=initial_not_ready,
        editable_after_upgrade=editable and post_count > pre_count,
    )
    proof = build_upgrade_proof(config.candidate, obs)
    target = common.ProofTarget(ROW_ID, config.candidate, config.out_path)
    return common.validate_and_write_proof(target, proof)


def _parse_args(argv: list[str] | None = None) -> UpgradeProbeConfig:
    parser = common.build_candidate_parser(__doc__ or "")
    parser.add_argument("--prior-rc-image-digest", required=True)
    args = parser.parse_args(argv)
    return UpgradeProbeConfig(
        common.candidate_from_args(args), args.prior_rc_image_digest, args.out, args.boot_timeout
    )


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    proof = run_upgrade_probe(config)
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
