"""Synthetic configuration-declaration controls; no deployment/client proof."""
from __future__ import annotations

import copy
import hashlib
import json
import unittest
from types import SimpleNamespace

import yaml

import mcp_ops_events_config as config

SOURCE = "a" * 40
RELEASE = "b" * 40
IMAGE = "ghcr.io/honua-io/honua-server@sha256:" + "1" * 64


def manifest():
    return {"platformRelease": "2026.1-rc.2", "candidate": {"ref": SOURCE, "refSource": "trunk"},
        "components": {"honua-server": {"sha": SOURCE,
            "image": "ghcr.io/honua-io/honua-server:nightly-aaaaaaa", "digest": "sha256:" + "1" * 64,
            "awsLambdaImage": "ghcr.io/honua-io/honua-server:nightly-lambda-aot-aaaaaaa-amd64",
            "awsLambdaDigest": "sha256:" + "2" * 64}}}


def selected(value=None, **kwargs):
    payload = yaml.safe_dump(value if value is not None else manifest()).encode()
    return config.declaration(payload, release_source_sha=kwargs.get("release", RELEASE),
        candidate_source_sha=kwargs.get("source", SOURCE), candidate_image=kwargs.get("image", IMAGE))


class OwnedConfigurationControls(unittest.TestCase):
    def test_real_selected_artifact_is_unchanged_and_alternate_is_explicit_configuration_only(self):
        result = selected()
        self.assertEqual(result["candidateImage"], IMAGE)
        self.assertEqual(result["artifactBindings"]["declaredServing"], IMAGE)
        self.assertNotEqual(result["artifactBindings"]["explicitTestTarget"], IMAGE)
        self.assertEqual(result["expectedSkewedIds"], ["cert-ops-skew"])
        self.assertFalse(result["actuationRequested"])
        self.assertFalse(result["deployedTargetObserved"])
        self.assertEqual(result["evidenceLevel"], "L1-declared-configuration")
        self.assertEqual(len(result["environment"]), 8)
        self.assertEqual(result["environment"]["ControlPlane__DeployTargets__0__RequiresApproval"], "true")
        self.assertFalse(any(word in key.lower() for key in result["environment"]
                             for word in ("password", "token", "credential", "region", "account", "role")))

    def test_both_original_manifest_and_exact_sorted_environment_are_hash_bound(self):
        raw = yaml.safe_dump(manifest()).encode()
        result = selected()
        self.assertEqual(result["platformManifestSha256"], "sha256:" + hashlib.sha256(raw).hexdigest())
        encoded = json.dumps(result["environment"], sort_keys=True, separators=(",", ":")).encode()
        self.assertEqual(result["environmentSha256"], "sha256:" + hashlib.sha256(encoded).hexdigest())

    def test_source_and_candidate_ref_substitution_refuse(self):
        for mutate in (lambda v: v["candidate"].update(ref="c" * 40),
                       lambda v: v["candidate"].update(refSource="feature"),
                       lambda v: v["components"]["honua-server"].update(sha="c" * 40)):
            value = manifest(); mutate(value)
            with self.assertRaises(ValueError):
                selected(value)
        for kwargs in ({"source": "short"}, {"release": "short"}, {"image": IMAGE + "0"},
                       {"image": "ghcr.io/honua-io/honua-server:nightly"}):
            with self.assertRaises(ValueError):
                selected(**kwargs)

    def test_absent_floating_unrelated_or_identical_alternate_artifacts_refuse(self):
        for key, replacement in (("awsLambdaDigest", None), ("awsLambdaDigest", "sha256:" + "1" * 64),
                                 ("awsLambdaDigest", "sha256:short"),
                                 ("awsLambdaImage", "ghcr.io/foreign/server:nightly"),
                                 ("image", "https://foreign.example/image")):
            with self.subTest(key=key, value=replacement):
                value = manifest(); value["components"]["honua-server"][key] = replacement
                with self.assertRaises(ValueError):
                    selected(value)

    def test_no_extra_config_parameters_or_manifest_mutation(self):
        value = manifest(); original = copy.deepcopy(value)
        result = selected(value)
        self.assertEqual(value, original)
        result["environment"]["ControlPlane__PlatformRelease__Version"] = "changed"
        self.assertEqual(selected(value)["environment"]["ControlPlane__PlatformRelease__Version"], "2026.1-rc.2")

    def test_unbounded_or_nonbyte_manifest_refuses(self):
        for value in ("text", b" " * 262145, b"[]", b"platformRelease: first\nplatformRelease: last\n",
                      b"components: {honua-server: {sha: first, sha: last}}\n", b"[malformed"):
            with self.assertRaises(ValueError):
                config.declaration(value, release_source_sha=RELEASE, candidate_source_sha=SOURCE, candidate_image=IMAGE)

    def test_actual_daemon_query_selects_only_eight_nonsecret_options(self):
        expected = selected()
        runtime = {"running": True, "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "containerId": "c" * 64}
        observed = []
        def run(command, **kwargs):
            observed.append((command, kwargs))
            self.assertEqual(command[:3], ["docker", "inspect", "--format"])
            self.assertEqual(command[-1], runtime["containerId"])
            self.assertNotIn("{{json .Config.Env}}", command[3])
            self.assertNotIn("BearerToken", command[3])
            self.assertEqual(kwargs["timeout"], 30)
            rows = [json.dumps({"selectedScope": True})] * 8
            rows += [json.dumps(key + "=" + value) for key, value in expected["environment"].items()]
            return SimpleNamespace(returncode=0, stdout="\n".join(rows))
        result = config.observe_configuration(expected, runtime, run=run)
        self.assertEqual(len(observed), 1)
        self.assertEqual(result["environment"], expected["environment"])
        self.assertEqual(result["environmentSha256"], expected["environmentSha256"])
        self.assertFalse(result["deployedTargetObserved"])

    def test_daemon_failure_wrong_values_missing_duplicate_or_extra_scope_refuse(self):
        expected = selected()
        runtime = {"running": True, "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "containerId": "c" * 64}
        values = [json.dumps(key + "=" + value) for key, value in expected["environment"].items()]
        marker = json.dumps({"selectedScope": True})
        cases = [(1, ""), (0, "x" * 32769), (0, "\n".join([marker] * 9 + values)),
                 (0, "\n".join([marker] * 8 + values[:-1])),
                 (0, "\n".join([marker] * 8 + values + [values[0]])),
                 (0, "\n".join([marker] * 8 + values[:-1] + [json.dumps("ControlPlane__DeployTargets__0__RequiresApproval=false")])),
                 (0, "\n".join([marker] * 8 + values + [json.dumps("unexpected=foreign")]))]
        for code, output in cases:
            with self.subTest(code=code, output=output[:100]), self.assertRaises(ValueError):
                config.observe_configuration(expected, runtime,
                    run=lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=output))


if __name__ == "__main__":
    unittest.main()
