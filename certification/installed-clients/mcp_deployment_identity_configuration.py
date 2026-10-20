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
            or not isinstance(runtime.get("containerId"),str) or not re.fullmatch(r"[a-f0-9]{64}", runtime["containerId"])
            or not isinstance(runtime.get("imageId"),str) or not re.fullmatch(r"sha256:[a-f0-9]{64}",runtime["imageId"])
            or not isinstance(runtime.get("candidateSourceSha"),str) or not re.fullmatch(r"[a-f0-9]{40}",runtime["candidateSourceSha"])):
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


def option_template():
    """Select safe values inside Docker; case-insensitive census emits no values."""
    def prefix(value, insensitive=False):
        selected="(slice . 0 "+str(len(value))+")"
        if insensitive: selected="(lower "+selected+")"; value=value.lower()
        return '(and (ge (len .) '+str(len(value))+') (eq '+selected+' '+json.dumps(value)+'))'
    scope='(or '+prefix("MultiTenancy__",True)+' '+prefix("Studio__EndUserAuthorization__",True)+')'
    template='{{range .Config.Env}}{{if '+scope+'}}{"selectedScope":true}{{println}}{{end}}'
    for key in ENV:
        template+='{{if '+prefix(key+'=')+'}}{{json .}}{{println}}{{end}}'
    return template+'{{end}}'


def observe_configuration(configuration, inspect_owned):
    before = inspect_owned()
    result = subprocess.run(["docker", "inspect", "--format", option_template(), before["containerId"]],
        check=True, capture_output=True, text=True, timeout=20)
    # The daemon emits only exact reviewed nonsecret options and namespace counts.
    if len(result.stdout)>32768: raise ValueError("selected environment observation oversized")
    lines=result.stdout.splitlines()
    # Docker adds one final newline after the template's own final newline.
    # Remove that one empty line only; malformed interior/repeated blanks refuse.
    if lines and lines[-1]=="": lines.pop()
    if any(line=="" for line in lines):
        raise ValueError("selected environment blank framing refused")
    selected, count = {}, 0
    for line in lines:
        value=json.loads(line)
        if value=={"selectedScope":True}: count+=1; continue
        if not isinstance(value,str) or "=" not in value:
            raise ValueError("selected environment framing malformed")
        key,value=value.split("=",1)
        if key not in ENV or key in selected:
            raise ValueError("undeclared or aliased authorization environment refused")
        selected[key]=value
    if count!=len(ENV):
        raise ValueError("unreviewed case-insensitive authorization configuration refused")
    after = inspect_owned()
    if any(before.get(key) != after.get(key) for key in ("containerId", "imageId", "candidateSourceSha")):
        raise ValueError("owned runtime changed during configuration observation")
    observation = {"schema": "honua.mcp-deployment-configuration-observation/v1",
        "declarationSha256": digest(configuration), "containerId": after["containerId"],
        "imageId": after["imageId"], "environment": selected,
        "observedAt": datetime.now(timezone.utc).isoformat()}
    verify_configuration(configuration, observation, after)
    return observation
