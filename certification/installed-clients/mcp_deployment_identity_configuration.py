"""Explicit owned test posture; configuration observation is not tenant proof."""
from __future__ import annotations
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone

ENV = {
    "MultiTenancy__Enabled": "true",
    "MultiTenancy__DefaultTenantId": "public",
    "MultiTenancy__MultiTenantAdminRoles__0": "admin",
    "MultiTenancy__MultiTenantAdminRoles__1": "multi_tenant_admin",
    "MultiTenancy__MultiTenantAdminRoles__2": "platform_admin",
    "Studio__EndUserAuthorization__Enabled": "true",
}

def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def declaration(tenant_a):
    if not isinstance(tenant_a, str) or not re.fullmatch(r"mcp-deployment-[a-f0-9]{12}", tenant_a):
        raise ValueError("owned deployment tenant must bind the fresh journey nonce")
    return {"schema": "honua.mcp-deployment-owned-authorization/v1", "environment": dict(ENV),
            "primaryTenant": tenant_a, "foreignTenant": "public", "foreignRole": "scoped-api-key",
            "roleGrants": [{"service": "Workspace", "layer": "*", "operation": "Read"},
                           {"service": "Deployment", "layer": "*", "operation": "Read"}]}


def verify_configuration(configuration, observation, runtime):
    """Rejudge exact normal environment; caller identities/resources are separate."""
    if not isinstance(configuration, dict) or configuration != declaration(configuration.get("primaryTenant")):
        raise ValueError("owned authorization declaration differs")
    if (not isinstance(runtime, dict) or runtime.get("running") is not True
            or not re.fullmatch(r"[a-f0-9]{64}", runtime.get("containerId", ""))):
        raise ValueError("owned candidate runtime missing")
    expected = {"schema", "declarationSha256", "containerId", "imageId", "environment", "observedAt"}
    if (not isinstance(observation, dict) or set(observation) != expected
            or observation["schema"] != "honua.mcp-deployment-configuration-observation/v1"
            or observation["declarationSha256"] != digest(configuration)
            or observation["containerId"] != runtime["containerId"]
            or observation["imageId"] != runtime.get("imageId")
            or observation["environment"] != configuration["environment"]):
        raise ValueError("actual owned container authorization configuration differs")
    observed = datetime.fromisoformat(observation["observedAt"].replace("Z", "+00:00"))
    if observed.tzinfo is None:
        raise ValueError("configuration observation lacks timezone")
    return {"primaryTenant": configuration["primaryTenant"], "foreignTenant": configuration["foreignTenant"],
            "configurationSha256": digest(configuration), "foreignRole": "scoped-api-key"}


def observe_configuration(configuration, inspect_owned):
    before = inspect_owned()
    result = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Env}}", before["containerId"]],
        check=True, capture_output=True, text=True, timeout=20)
    # Never retain the complete environment, which can contain credentials.
    values = json.loads(result.stdout)
    if not isinstance(values, list) or not all(isinstance(value, str) and "=" in value for value in values):
        raise ValueError("actual owned container environment malformed")
    selected = {}
    prefixes = ("multitenancy__", "studio__enduserauthorization__")
    for entry in values:
        key, value = entry.split("=", 1)
        if key.lower().startswith(prefixes):
            if key not in ENV or key in selected:
                raise ValueError("undeclared or aliased authorization environment refused")
            selected[key] = value
    after = inspect_owned()
    if any(before.get(key) != after.get(key) for key in ("containerId", "imageId", "candidateSourceSha")):
        raise ValueError("owned runtime changed during configuration observation")
    observation = {"schema": "honua.mcp-deployment-configuration-observation/v1",
        "declarationSha256": digest(configuration), "containerId": after["containerId"],
        "imageId": after["imageId"], "environment": selected,
        "observedAt": datetime.now(timezone.utc).isoformat()}
    verify_configuration(configuration, observation, after)
    return observation
