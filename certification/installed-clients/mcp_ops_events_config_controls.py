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
        self.assertEqual(len(result["environment"]), 10)
        self.assertEqual(result["environment"]["ControlPlane__PlatformRelease__Workers__0__RuntimeProfile"], "managed")
        self.assertEqual(result["environment"]["ControlPlane__PlatformRelease__Workers__0__ArtifactReference"], IMAGE)
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

    def test_release_co_versions_serving_and_a_digest_bound_worker(self):
        # honua-server refuses a release declaring serving without workers; the
        # managed worker is the governed serving index itself, never a tag.
        environment = selected()["environment"]
        prefix = "ControlPlane__PlatformRelease__Workers__"
        workers = {key: value for key, value in environment.items() if key.startswith(prefix)}
        self.assertEqual(set(workers), {prefix + "0__RuntimeProfile", prefix + "0__ArtifactReference"})
        self.assertRegex(workers[prefix + "0__ArtifactReference"], r"^ghcr\.io/honua-io/honua-server@sha256:[a-f0-9]{64}$")
        self.assertEqual(workers[prefix + "0__ArtifactReference"],
                         environment["ControlPlane__PlatformRelease__ServingArtifactReference"])
        self.assertNotEqual(workers[prefix + "0__ArtifactReference"],
                            environment["ControlPlane__DeployTargets__0__ArtifactReference"])

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

    def test_actual_daemon_query_selects_only_the_ten_nonsecret_options(self):
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
            rows = [json.dumps({"selectedScope": True})] * 10
            rows += [json.dumps(key + "=" + value) for key, value in expected["environment"].items()]
            return SimpleNamespace(returncode=0, stdout="\n".join(rows) + "\n\n")
        result = config.observe_configuration(expected, runtime, run=run)
        self.assertEqual(len(observed), 1)
        self.assertEqual(result["environment"], expected["environment"])
        self.assertEqual(result["environmentSha256"], expected["environmentSha256"])
        self.assertFalse(result["deployedTargetObserved"])

    def test_nightly_candidate_advance_binds_selected_candidate_against_governed_skew_target(self):
        advanced_source = "f" * 40
        advanced_image = "ghcr.io/honua-io/honua-server@sha256:" + "9" * 64
        payload = yaml.safe_dump(manifest()).encode()
        with self.assertRaises(ValueError):
            config.declaration(payload, release_source_sha=RELEASE,
                candidate_source_sha=advanced_source, candidate_image=advanced_image)
        result = config.declaration_for_nightly_advance(payload, release_source_sha=RELEASE,
            candidate_source_sha=advanced_source, candidate_image=advanced_image)
        self.assertEqual(result["candidateSourceSha"], advanced_source)
        self.assertEqual(result["candidateImage"], advanced_image)
        self.assertEqual(result["artifactBindings"]["declaredServing"], advanced_image)
        self.assertEqual(result["environment"]["ControlPlane__PlatformRelease__ServingArtifactReference"],
                         advanced_image)
        self.assertEqual(result["environment"]["ControlPlane__PlatformRelease__Workers__0__ArtifactReference"],
                         advanced_image)
        self.assertEqual(result["artifactBindings"]["explicitTestTarget"],
                         "ghcr.io/honua-io/honua-server@sha256:" + "2" * 64)
        self.assertEqual(result["expectedSkewedIds"], ["cert-ops-skew"])
        # Exact-match nightly run (no new commits) also succeeds with declaration_for_nightly_advance.
        same = config.declaration_for_nightly_advance(payload, release_source_sha=RELEASE,
            candidate_source_sha=SOURCE, candidate_image=IMAGE)
        self.assertEqual(same, selected())

    def test_nightly_candidate_advance_refuses_corrupt_manifest_or_skew_collision(self):
        advanced_source = "f" * 40
        advanced_image = "ghcr.io/honua-io/honua-server@sha256:" + "9" * 64
        payload = yaml.safe_dump(manifest()).encode()
        for bad_image in ("ghcr.io/honua-io/honua-server@sha256:" + "2" * 64,
                          "ghcr.io/other/honua-server@sha256:" + "9" * 64,
                          "ghcr.io/honua-io/honua-server:nightly"):
            with self.subTest(bad_image=bad_image), self.assertRaises(ValueError):
                config.declaration_for_nightly_advance(payload, release_source_sha=RELEASE,
                    candidate_source_sha=advanced_source, candidate_image=bad_image)
        for mutate in (lambda v: v["candidate"].update(ref="e" * 40),
                       lambda v: v["candidate"].update(refSource="branch"),
                       lambda v: v["candidate"].update(extra="unexpected"),
                       lambda v: v["components"]["honua-server"].update(sha="e" * 40),
                       lambda v: v["components"]["honua-server"].update(awsLambdaDigest="sha256:" + "1" * 64)):
            value = manifest(); mutate(value)
            with self.assertRaises(ValueError):
                config.declaration_for_nightly_advance(yaml.safe_dump(value).encode(), release_source_sha=RELEASE,
                    candidate_source_sha=advanced_source, candidate_image=advanced_image)

    def test_cli_allow_candidate_advance_flag_gates_unpinned_candidate(self):
        import subprocess, sys, tempfile
        from pathlib import Path
        advanced_source = "f" * 40
        advanced_image = "ghcr.io/honua-io/honua-server@sha256:" + "9" * 64
        script = Path(config.__file__).resolve()
        with tempfile.TemporaryDirectory() as work:
            manifest_path = Path(work) / "platform-manifest.yaml"
            manifest_path.write_bytes(yaml.safe_dump(manifest()).encode())
            base = [sys.executable, str(script), "--manifest", str(manifest_path),
                    "--release-source-sha", RELEASE, "--candidate-source-sha", advanced_source,
                    "--candidate-image", advanced_image]
            refused = subprocess.run(base, capture_output=True, text=True, timeout=30)
            self.assertNotEqual(refused.returncode, 0)
            accepted = subprocess.run(base + ["--allow-candidate-advance"], capture_output=True, text=True, timeout=30)
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            parsed = json.loads(accepted.stdout)
            self.assertEqual(parsed["candidateSourceSha"], advanced_source)
            self.assertEqual(parsed["candidateImage"], advanced_image)

    def test_daemon_failure_wrong_values_missing_duplicate_or_extra_scope_refuse(self):
        expected = selected()
        runtime = {"running": True, "candidateSourceSha": SOURCE, "candidateImage": IMAGE, "containerId": "c" * 64}
        values = [json.dumps(key + "=" + value) for key, value in expected["environment"].items()]
        marker = json.dumps({"selectedScope": True})
        cases = [(1, ""), (0, "x" * 32769),
                 (0, "\n" + "\n".join([marker] * 10 + values)),
                 (0, "\n".join([marker] * 10 + values) + "\n\n\n"), (0, "\n".join([marker] * 11 + values)),
                 (0, "\n".join([marker] * 10 + values[:-1])),
                 (0, "\n".join([marker] * 10 + values + [values[0]])),
                 (0, "\n".join([marker] * 10 + values[:-1] + [json.dumps("ControlPlane__DeployTargets__0__RequiresApproval=false")])),
                 (0, "\n".join([marker] * 10 + values + [json.dumps("unexpected=foreign")]))]
        for code, output in cases:
            with self.subTest(code=code, output=output[:100]), self.assertRaises(ValueError):
                config.observe_configuration(expected, runtime,
                    run=lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout=output))


if __name__ == "__main__":
    unittest.main()
