#!/usr/bin/env python3
"""Live Docker probe for RC2 single-tenant Console parity and multi-tenant isolation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import urllib.parse

import rc2_live_docker as docker_env
import rc2_live_probes_common as common

ROW_ID = "tenant-isolation-parity"
SERVICE_NAME = "mask_probe"
SECRET_FIELD = "secret_note"
SECRET_SENTINEL = "classified-"

HOST_ADMIN_ROUTE_PROBES: tuple[tuple[str, str], ...] = (
    ("/api/v1/admin/connections", "/api/v1/admin/connections"),
    ("/api/v1/admin/roles", "/api/v1/admin/roles"),
    ("/api/v1/admin/rls-policies", "/api/v1/admin/rls-policies"),
    ("/api/v1/admin/field-mask-policies", "/api/v1/admin/field-mask-policies"),
    ("/api/v1/console/access/*", "/api/v1/console/access/default/roles"),
    ("/api/v1/admin/operations/streaming", "/api/v1/admin/operations/streaming/subscribers"),
    ("/api/v1/admin/observability/*", "/api/v1/admin/observability/telemetry"),
    ("/api/v1/admin/cache/*", "/api/v1/admin/cache/status"),
)

SINGLE_TENANT_WORKFLOWS: tuple[str, ...] = (
    "/api/v1/console/session",
    "/api/v1/console/content",
    "/api/v1/admin/connections",
    "/api/v1/admin/roles",
    "/api/v1/admin/rls-policies",
    "/api/v1/admin/field-mask-policies",
    "/api/v1/console/access/default/roles",
    "/api/v1/admin/operations/streaming/subscribers",
    "/api/v1/admin/observability/telemetry",
    "/api/v1/admin/cache/status",
)


@dataclass(frozen=True)
class TenantProbeConfig:
    """Configuration for executing the live tenant isolation and parity probe."""

    candidate: common.CandidateSpec
    out_path: Path
    boot_timeout: float = common.DEFAULT_BOOT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class TenantTokens:
    """HS256 bearer tokens representing platform admin, tenants A/B, and masked reader."""

    platform_admin: str
    tenant_a_admin: str
    tenant_b_admin: str
    masked_reader: str


@dataclass(frozen=True)
class TenantObservation:
    """Raw live observations from single-tenant parity and multi-tenant isolation."""

    single_tenant_zero_403: bool
    single_tenant_workflows_passed: int
    platform_admin_succeeded: bool
    tenant_admin_allowed_host_routes: int
    cross_tenant_leaks: int
    platform_admin_routes: list[str]
    hidden_field_protocols_blocked: list[str]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mint_probe_tokens() -> TenantTokens:
    return TenantTokens(
        platform_admin=common.mint_hs256_jwt({"sub": "platform-admin", "role": ["admin", "platform_admin"]}),
        tenant_a_admin=common.mint_hs256_jwt({"sub": "tenant-a-admin", "role": "admin", "tid": "tenant-a", "scope": "honua.mcp.full"}),
        tenant_b_admin=common.mint_hs256_jwt({"sub": "tenant-b-admin", "role": "admin", "tid": "tenant-b", "scope": "honua.mcp.full"}),
        masked_reader=common.mint_hs256_jwt({"sub": "masked-reader", "role": "masked_reader", "tid": "tenant-a"}),
    )


def _probe_single_tenant_parity(base_url: str) -> tuple[bool, int]:
    headers = {"X-API-Key": common.ADMIN_PASSWORD}
    zero_403 = True
    passed = 0
    for path in SINGLE_TENANT_WORKFLOWS:
        reply = common.http_request(common.HttpSpec("GET", f"{base_url}{path}", headers))
        if reply.status == common.HTTP_FORBIDDEN:
            zero_403 = False
        if reply.status == common.HTTP_OK:
            passed += 1
    return zero_403, passed


def _probe_host_admin_routes(
    base_url: str, tokens: TenantTokens
) -> tuple[bool, int, list[str]]:
    platform_ok = True
    allowed_for_tenant = 0
    verified_routes: list[str] = []
    for route_key, concrete_path in HOST_ADMIN_ROUTE_PROBES:
        tenant_reply = common.http_request(
            common.HttpSpec("GET", f"{base_url}{concrete_path}", _bearer(tokens.tenant_a_admin))
        )
        if tenant_reply.status != common.HTTP_FORBIDDEN:
            allowed_for_tenant += 1
        platform_reply = common.http_request(
            common.HttpSpec("GET", f"{base_url}{concrete_path}", _bearer(tokens.platform_admin))
        )
        if platform_reply.status == common.HTTP_OK:
            verified_routes.append(route_key)
        else:
            platform_ok = False
    return platform_ok, allowed_for_tenant, sorted(verified_routes)


def _extract_draft_id(created: object) -> str | None:
    if not isinstance(created, dict):
        return None
    data = created.get("data") if isinstance(created.get("data"), dict) else created
    draft_id = data.get("draftId")
    if isinstance(draft_id, str) and draft_id:
        return draft_id
    nested = data.get("draft")
    return nested["draftId"] if isinstance(nested, dict) and isinstance(nested.get("draftId"), str) else None


def _create_tenant_a_draft(
    base_url: str, tokens: TenantTokens, published: common.PublishedLayer
) -> str:
    draft_payload = {
        "packageKey": "tenant-a-isolated-map",
        "envelope": {
            "family": "map",
            "schemaVersion": "1.0",
            "format": "honua_map.v1",
            "body": {"title": "Tenant A Map", "service": published.service_name, "layerId": published.layer_id},
        },
    }
    headers = {**_bearer(tokens.tenant_a_admin), "Content-Type": "application/json"}
    reply, created = common.json_request(
        common.HttpSpec("POST", f"{base_url}/api/v1/studio/package-drafts", headers, json.dumps(draft_payload).encode())
    )
    draft_id = _extract_draft_id(created)
    if reply.status not in (common.HTTP_OK, common.HTTP_CREATED) or not draft_id:
        raise RuntimeError(f"tenant-a draft creation failed: HTTP {reply.status} {reply.body[:300]!r}")
    return draft_id


def _probe_studio_tenant_isolation(
    base_url: str, tokens: TenantTokens, published: common.PublishedLayer
) -> int:
    draft_id = _create_tenant_a_draft(base_url, tokens, published)
    leaks = 0
    own = common.http_request(
        common.HttpSpec("GET", f"{base_url}/api/v1/studio/package-drafts/{draft_id}", _bearer(tokens.tenant_a_admin))
    )
    if own.status != common.HTTP_OK:
        leaks += 1
    cross_get = common.http_request(
        common.HttpSpec("GET", f"{base_url}/api/v1/studio/package-drafts/{draft_id}", _bearer(tokens.tenant_b_admin))
    )
    if cross_get.status == common.HTTP_OK:
        leaks += 1
    cross_list = common.http_request(
        common.HttpSpec("GET", f"{base_url}/api/v1/studio/package-drafts", _bearer(tokens.tenant_b_admin))
    )
    if draft_id in cross_list.body.decode("utf-8", errors="replace"):
        leaks += 1
    return leaks


def _create_field_mask_policy(base_url: str, platform_token: str) -> None:
    headers = {**_bearer(platform_token), "Content-Type": "application/json"}
    payload = {
        "role": "masked_reader",
        "service": "*",
        "layer": "*",
        "attribute": SECRET_FIELD,
        "description": "RC2 live hidden-column verification across protocols",
    }
    reply = common.http_request(
        common.HttpSpec("POST", f"{base_url}/api/v1/admin/field-mask-policies", headers, json.dumps(payload).encode())
    )
    if reply.status not in (common.HTTP_OK, common.HTTP_CREATED):
        raise RuntimeError(f"create field-mask policy failed: HTTP {reply.status} {reply.body[:300]!r}")


def _records_hide_secret(reply: common.HttpReply, parsed: object, records_key: str) -> bool:
    if reply.status != common.HTTP_OK or not isinstance(parsed, dict) or not parsed.get(records_key):
        return False
    text = reply.body.decode("utf-8", errors="replace").lower()
    return SECRET_FIELD not in text and SECRET_SENTINEL not in text


def _geoservices_hides_secret(reply: common.HttpReply, parsed: object) -> bool:
    if reply.status != common.HTTP_OK or not isinstance(parsed, dict):
        return False
    features = parsed.get("features")
    if not isinstance(features, list) or not features:
        return False
    if SECRET_SENTINEL in reply.body.decode("utf-8", errors="replace").lower():
        return False
    return all(
        isinstance(item, dict)
        and isinstance(item.get("attributes"), dict)
        and SECRET_FIELD not in {k.lower() for k in item["attributes"]}
        for item in features
    )


def _discover_stac_collection(base_url: str, headers: dict[str, str]) -> str | None:
    reply, parsed = common.json_request(common.HttpSpec("GET", f"{base_url}/stac/collections", headers))
    if reply.status != common.HTTP_OK or not isinstance(parsed, dict):
        return None
    collections = parsed.get("collections")
    if isinstance(collections, list) and collections:
        first = collections[0]
        return first["id"] if isinstance(first, dict) and isinstance(first.get("id"), str) else None
    return None


def _check_stac_and_analytics(
    base_url: str, published: common.PublishedLayer, headers: dict[str, str]
) -> list[str]:
    blocked: list[str] = []
    stac_col = _discover_stac_collection(base_url, headers)
    if stac_col:
        stac_r, stac = common.json_request(
            common.HttpSpec("GET", f"{base_url}/stac/collections/{urllib.parse.quote(stac_col)}/items", headers)
        )
        if _records_hide_secret(stac_r, stac, "features"):
            blocked.append("stac")
    cluster_body = json.dumps({
        "algorithm": "dbscan", "eps": 50000, "minPoints": 1,
        "returnMode": "features", "outFields": "*",
    }).encode()
    sa_url = f"{base_url}/rest/services/{published.service_name}/FeatureServer/{published.layer_id}/queryClusters"
    sa_r, sa = common.json_request(
        common.HttpSpec("POST", sa_url, {**headers, "Content-Type": "application/json"}, cluster_body)
    )
    if _records_hide_secret(sa_r, sa, "features"):
        blocked.append("spatial-analytics")
    return blocked


def _probe_hidden_field_protocols(
    base_url: str, published: common.PublishedLayer, tokens: TenantTokens
) -> list[str]:
    svc, lid = published.service_name, published.layer_id
    unmasked_url = f"{base_url}/rest/services/{svc}/FeatureServer/{lid}/query?where=1=1&outFields=*&f=json"
    unmasked_r = common.http_request(
        common.HttpSpec("GET", unmasked_url, {**_bearer(tokens.platform_admin), "X-Honua-Tenant": "tenant-a"})
    )
    if SECRET_SENTINEL not in unmasked_r.body.decode("utf-8", errors="replace"):
        raise RuntimeError("unmasked admin query did not return seeded secret_note baseline")
    headers = _bearer(tokens.masked_reader)
    blocked: list[str] = []
    geo_r, geo = common.json_request(common.HttpSpec("GET", unmasked_url, headers))
    if _geoservices_hides_secret(geo_r, geo):
        blocked.append("geoservices")
    ogc_r, ogc = common.json_request(common.HttpSpec("GET", f"{base_url}/ogc/features/collections/{lid}/items", headers))
    if _records_hide_secret(ogc_r, ogc, "features"):
        blocked.append("ogc")
    odata_r, odata = common.json_request(common.HttpSpec("GET", f"{base_url}/odata/Features({lid})", headers))
    if _records_hide_secret(odata_r, odata, "value"):
        blocked.append("odata")
    blocked.extend(_check_stac_and_analytics(base_url, published, headers))
    return sorted(blocked)


def build_tenant_proof(
    spec: common.CandidateSpec, obs: TenantObservation, observed_at: str | None = None
) -> dict[str, object]:
    """Assemble the fail-closed tenant-isolation-parity proof JSON payload."""
    return {
        **common.build_identity_fields(spec, observed_at),
        "singleTenantConsoleZero403": obs.single_tenant_zero_403,
        "singleTenantWorkflowsPassed": obs.single_tenant_workflows_passed,
        "platformAdminSucceeded": obs.platform_admin_succeeded,
        "tenantAdminAllowedHostRoutes": obs.tenant_admin_allowed_host_routes,
        "crossTenantLeaks": obs.cross_tenant_leaks,
        "multiTenantPlatformAdminRequiredRoutes": obs.platform_admin_routes,
        "hiddenFieldProtocolsBlocked": obs.hidden_field_protocols_blocked,
    }


def _run_multi_tenant_phase(
    stack: docker_env.DockerStack, candidate_image: str, timeout: float
) -> tuple[bool, int, int, list[str], list[str]]:
    mt_env = ("MultiTenancy__Enabled=true", "Licensing__Mode=Disabled", *common.oidc_env_vars())
    docker_env.start_honua_container(docker_env.HonuaLaunchSpec(stack, candidate_image, mt_env))
    common.wait_http_status(f"{stack.base_url}/healthz/ready", common.HTTP_OK, timeout)
    tokens = _mint_probe_tokens()
    platform_ok, allowed_host, routes = _probe_host_admin_routes(stack.base_url, tokens)
    docker_env.seed_probe_table(stack.pg_container)
    publish_headers = {**_bearer(tokens.platform_admin), "X-Honua-Tenant": "tenant-a"}
    published = docker_env.publish_postgis_layer(
        docker_env.PublishSpec(
            stack.base_url, stack.pg_container, SERVICE_NAME, editable=False, auth_headers=publish_headers
        )
    )
    leaks = _probe_studio_tenant_isolation(stack.base_url, tokens, published)
    _create_field_mask_policy(stack.base_url, tokens.platform_admin)
    blocked = _probe_hidden_field_protocols(stack.base_url, published, tokens)
    return platform_ok, allowed_host, leaks, routes, blocked


def run_tenant_probe(config: TenantProbeConfig) -> dict[str, object]:
    """Execute live single-tenant Console parity and multi-tenant isolation probes."""
    candidate_image = common.verify_candidate_image(config.candidate)
    stack = docker_env.create_network_and_postgis(
        "rc2-tenant", config.candidate.postgis_image, config.boot_timeout
    )
    try:
        docker_env.start_honua_container(
            docker_env.HonuaLaunchSpec(stack, candidate_image, ("MultiTenancy__Enabled=false",))
        )
        common.wait_http_status(f"{stack.base_url}/healthz/ready", common.HTTP_OK, config.boot_timeout)
        st_zero_403, st_passed = _probe_single_tenant_parity(stack.base_url)
        common.run_docker(["rm", "-f", stack.app_container])
        platform_ok, allowed_host, leaks, routes, blocked = _run_multi_tenant_phase(
            stack, candidate_image, config.boot_timeout
        )
    finally:
        docker_env.cleanup_stack(stack)
    obs = TenantObservation(
        single_tenant_zero_403=st_zero_403,
        single_tenant_workflows_passed=st_passed,
        platform_admin_succeeded=platform_ok,
        tenant_admin_allowed_host_routes=allowed_host,
        cross_tenant_leaks=leaks,
        platform_admin_routes=routes,
        hidden_field_protocols_blocked=blocked,
    )
    proof = build_tenant_proof(config.candidate, obs)
    target = common.ProofTarget(ROW_ID, config.candidate, config.out_path)
    return common.validate_and_write_proof(target, proof)


def _parse_args(argv: list[str] | None = None) -> TenantProbeConfig:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-sha", required=True)
    parser.add_argument("--server-digest", required=True)
    parser.add_argument("--amd64-digest", required=True)
    parser.add_argument("--lock-digest", required=True)
    parser.add_argument("--postgis-image", default=common.DEFAULT_POSTGIS_IMAGE)
    parser.add_argument("--boot-timeout", type=float, default=common.DEFAULT_BOOT_TIMEOUT_SECONDS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    candidate = common.CandidateSpec(
        args.server_sha, args.server_digest, args.amd64_digest,
        args.lock_digest, args.postgis_image,
    )
    return TenantProbeConfig(candidate, args.out, args.boot_timeout)


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    proof = run_tenant_probe(config)
    print(json.dumps(proof, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
