"""Owned, read-only L1 configuration-skew fixture from governed artifacts.

This declares a deliberately inconsistent test target. It does not deploy a
target, alter the selected candidate artifact, or grant operation approval.
The real candidate must retain and independently expose the same declaration.
"""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from datetime import datetime, timezone

import yaml

SCHEMA = "honua.mcp-ops-config-declaration/v1"
TARGET = "cert-ops-skew"
SELECTED_KEYS = (
    "ControlPlane__PlatformRelease__Version", "ControlPlane__PlatformRelease__ServingArtifactReference",
    "ControlPlane__DeployTargets__0__TargetId", "ControlPlane__DeployTargets__0__TargetKind",
    "ControlPlane__DeployTargets__0__Environment", "ControlPlane__DeployTargets__0__TargetName",
    "ControlPlane__DeployTargets__0__ArtifactReference", "ControlPlane__DeployTargets__0__RequiresApproval")


class UniqueManifestLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        self.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("governed manifest contains duplicate or non-string mapping keys")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def digest_reference(image, digest):
    if (not isinstance(image, str) or not re.fullmatch(r"ghcr\.io/honua-io/honua-server:[A-Za-z0-9_.-]+", image)
            or not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest)):
        raise ValueError("configuration fixture requires the governed immutable server artifacts")
    return image.rsplit(":", 1)[0] + "@" + digest


def declaration(manifest_bytes, *, release_source_sha, candidate_source_sha, candidate_image):
    if not isinstance(manifest_bytes, bytes) or len(manifest_bytes) > 262144:
        raise ValueError("configuration declaration requires original bounded manifest bytes")
    if any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value)
           for value in (release_source_sha, candidate_source_sha)):
        raise ValueError("configuration declaration requires exact protected source identities")
    try:
        manifest = yaml.load(manifest_bytes, Loader=UniqueManifestLoader)
    except yaml.YAMLError as error:
        raise ValueError("governed platform manifest is malformed") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("components"), dict):
        raise ValueError("governed platform manifest is missing")
    server = manifest["components"].get("honua-server")
    if (not isinstance(server, dict) or server.get("sha") != candidate_source_sha
            or manifest.get("candidate") != {"ref": candidate_source_sha, "refSource": "trunk"}
            or not isinstance(manifest.get("platformRelease"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", manifest["platformRelease"])):
        raise ValueError("governed manifest is not bound to the actual selected candidate")
    serving = digest_reference(server.get("image"), server.get("digest"))
    alternate = digest_reference(server.get("awsLambdaImage"), server.get("awsLambdaDigest"))
    if candidate_image != serving or serving == alternate:
        raise ValueError("candidate or deliberately different governed configuration artifact is missing")
    # These are declared options only. No AWS region/account/role, backend
    # credentials, operation gateway request, actuation or proposal is supplied.
    selected = {
        "ControlPlane__PlatformRelease__Version": manifest["platformRelease"],
        "ControlPlane__PlatformRelease__ServingArtifactReference": serving,
        "ControlPlane__DeployTargets__0__TargetId": TARGET,
        "ControlPlane__DeployTargets__0__TargetKind": "AwsLambda",
        "ControlPlane__DeployTargets__0__Environment": "certification-disposable",
        "ControlPlane__DeployTargets__0__TargetName": TARGET,
        "ControlPlane__DeployTargets__0__ArtifactReference": alternate,
        "ControlPlane__DeployTargets__0__RequiresApproval": "true",
    }
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    return {"schema": SCHEMA, "releaseSourceSha": release_source_sha,
        "platformManifestSha256": "sha256:" + hashlib.sha256(manifest_bytes).hexdigest(),
        "candidateSourceSha": candidate_source_sha, "candidateImage": candidate_image,
        "environment": selected, "environmentSha256": "sha256:" + hashlib.sha256(encoded).hexdigest(),
        "artifactBindings": {"declaredServing": serving, "explicitTestTarget": alternate},
        "expectedSkewedIds": [TARGET], "evidenceLevel": "L1-declared-configuration",
        "deployedTargetObserved": False, "actuationRequested": False}


def option_template():
    # Docker's template selects these non-secret values in the daemon. Never
    # capture Config.Env wholesale: unrelated credentials must not leave it.
    def starts(prefix, *, insensitive=False):
        selected = f"(slice . 0 {len(prefix)})"
        if insensitive:
            selected = "(lower " + selected + ")"
            prefix = prefix.lower()
        return f'(and (ge (len .) {len(prefix)}) (eq {selected} {json.dumps(prefix)}))'
    scope = '(or ' + starts('ControlPlane__PlatformRelease__', insensitive=True) + ' ' + starts('ControlPlane__DeployTargets__', insensitive=True) + ')'
    template = '{{range .Config.Env}}{{if ' + scope + '}}{"selectedScope":true}{{println}}{{end}}'
    for key in SELECTED_KEYS:
        template += '{{if ' + starts(key + '=') + '}}{{json .}}{{println}}{{end}}'
    return template + '{{end}}'


def observe_configuration(expected, runtime, *, run=subprocess.run):
    if (not isinstance(expected, dict) or expected.get("schema") != SCHEMA
            or not isinstance(expected.get("environment"), dict)
            or set(expected["environment"]) != set(SELECTED_KEYS)
            or not isinstance(runtime, dict) or runtime.get("running") is not True
            or runtime.get("candidateSourceSha") != expected.get("candidateSourceSha")
            or runtime.get("candidateImage") != expected.get("candidateImage")
            or not isinstance(runtime.get("containerId"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", runtime["containerId"])):
        raise ValueError("configuration observation requires exact declared candidate/runtime")
    observed_at = datetime.now(timezone.utc).isoformat()
    result = run(["docker", "inspect", "--format", option_template(), runtime["containerId"]],
        capture_output=True, text=True, timeout=30)
    if result.returncode or len(result.stdout) > 32768:
        raise ValueError("actual selected configuration readback failed")
    selected, count = {}, 0
    lines = result.stdout.splitlines()
    # Docker adds a CLI newline after the template's final println. Accept
    # that single framing line only; interior or repeated blanks still refuse.
    if lines and lines[-1] == "":
        lines.pop()
    for line in lines:
        value = json.loads(line)
        if value == {"selectedScope": True}:
            count += 1
            continue
        if not isinstance(value, str) or "=" not in value:
            raise ValueError("actual selected configuration readback is malformed")
        key, content = value.split("=", 1)
        if key not in SELECTED_KEYS or key in selected:
            raise ValueError("actual selected configuration is unknown or duplicated")
        selected[key] = content
    encoded = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
    if count != len(SELECTED_KEYS) or selected != expected["environment"] or digest != expected["environmentSha256"]:
        raise ValueError("actual candidate option values/coverage differ from declaration")
    return {"schema": "honua.mcp-ops-config-observation/v1", "observedAt": observed_at,
        "runtimeBinding": runtime, "environment": selected, "environmentSha256": digest,
        "selectedScopeEntryCount": count, "deployedTargetObserved": False, "actuationRequested": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--release-source-sha", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    args = parser.parse_args()
    try:
        result = declaration(args.manifest.read_bytes(), release_source_sha=args.release_source_sha,
            candidate_source_sha=args.candidate_source_sha, candidate_image=args.candidate_image)
        print(json.dumps(result, allow_nan=False)); return 0
    except (ValueError, TypeError, KeyError, OSError):
        print("owned findings configuration declaration refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
