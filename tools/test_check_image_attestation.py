import json
from pathlib import Path

import pytest
import yaml

from check_image_attestation import AttestationMismatch, check, main

HERE = Path(__file__).parent
SHA = "7ba422672e0c751843b17beb36e954a019cc19fb"
DIGEST = "sha256:dd50cd81c057e37e73a6144572abdfc90d48de314d7625c54c4ef3b6eb65b0fd"
# A different trunk build: the image a stale or hand-edited digest would point at.
OTHER_SHA = "ff1a463c0b5a1d6e2f4a9c8b7d6e5f4a3b2c1d0e"
OTHER_DIGEST = "sha256:" + "ab" * 32


TAG_REF = "refs/tags/nightly-candidate-" + SHA[:7]


def verified(sha=SHA, digest=DIGEST, dependency_sha=None, ref="refs/heads/trunk", signer_ref=None):
    """The shape `gh attestation verify --format json` emits for a nightly-container-build image."""
    return [{
        "verificationResult": {
            "signature": {"certificate": {
                "sourceRepositoryURI": "https://github.com/honua-io/honua-server",
                "sourceRepositoryDigest": sha,
                "sourceRepositoryRef": ref,
                "buildSignerURI": "https://github.com/honua-io/honua-server/.github/workflows/"
                                  "nightly-container-build.yml@" + str(signer_ref or ref),
            }},
            "statement": {
                "subject": [{"name": "ghcr.io/honua-io/honua-server",
                             "digest": {"sha256": digest.partition(":")[2]}}],
                "predicate": {"buildDefinition": {"resolvedDependencies": [{
                    "uri": "git+https://github.com/honua-io/honua-server@refs/heads/trunk",
                    "digest": {"gitCommit": dependency_sha or sha},
                }]}},
            },
        },
    }]


def manifest(tmp_path, sha=SHA, digest=DIGEST):
    path = tmp_path / "platform-manifest.yaml"
    path.write_text(yaml.safe_dump({"components": {"honua-server": {
        "sha": sha, "digest": digest, "image": "ghcr.io/honua-io/honua-server:nightly-aot-" + sha[:7],
    }}}), encoding="utf-8")
    return path


def test_attestation_from_the_manifest_source_is_accepted():
    assert check(verified(), SHA, DIGEST) == 1


def test_attestation_signed_at_the_candidate_tag_for_the_manifest_sha_is_accepted():
    # honua-server#5791: the nightly tags the certified sha and re-dispatches from that tag.
    assert check(verified(ref=TAG_REF), SHA, DIGEST) == 1


@pytest.mark.parametrize("ref", [
    "refs/tags/nightly-candidate-" + OTHER_SHA[:7],   # the tag of a different candidate
    "refs/tags/nightly-candidate-" + SHA[:8],         # not exactly the first 7 characters
    "refs/tags/nightly-candidate-" + SHA,             # the full sha is not the tag shape
    "refs/tags/" + SHA[:7],
    "refs/heads/feature",
    "refs/heads/trunk-evil",
    "",
    None,
])
def test_attestation_from_any_other_ref_is_refused(ref):
    with pytest.raises(AttestationMismatch, match="was built from ref"):
        check(verified(ref=ref), SHA, DIGEST)


def test_signer_must_run_at_the_attested_source_ref():
    with pytest.raises(AttestationMismatch, match="did not run at the attested source ref"):
        check(verified(ref=TAG_REF, signer_ref="refs/heads/feature"), SHA, DIGEST)


def test_image_built_from_a_different_trunk_commit_is_refused():
    # The manifest pins sha A beside the digest of build B: B's attestation names B's source.
    with pytest.raises(AttestationMismatch, match="not the manifest server sha"):
        check(verified(sha=OTHER_SHA), SHA, DIGEST)


def test_resolved_source_dependency_must_also_be_the_manifest_sha():
    with pytest.raises(AttestationMismatch, match="resolves source commit"):
        check(verified(dependency_sha=OTHER_SHA), SHA, DIGEST)


def test_attestation_for_another_subject_is_refused():
    with pytest.raises(AttestationMismatch, match="manifest image digest"):
        check(verified(digest=OTHER_DIGEST), SHA, DIGEST)


def test_every_returned_attestation_must_be_bound():
    with pytest.raises(AttestationMismatch, match="attestation 1 was built from source"):
        check(verified() + verified(sha=OTHER_SHA), SHA, DIGEST)


@pytest.mark.parametrize("output", [[], {}, None])
def test_empty_verification_output_is_refused(output):
    with pytest.raises(AttestationMismatch, match="no verified attestation"):
        check(output, SHA, DIGEST)


def test_cli_fails_when_manifest_sha_and_digest_are_different_builds(tmp_path, capsys):
    attestation = tmp_path / "image-attestation.json"
    attestation.write_text(json.dumps(verified(sha=OTHER_SHA)), encoding="utf-8")
    assert main(["--manifest", str(manifest(tmp_path)), "--attestation", str(attestation)]) == 1
    assert "not bound to the candidate" in capsys.readouterr().err

    attestation.write_text(json.dumps(verified()), encoding="utf-8")
    assert main(["--manifest", str(manifest(tmp_path)), "--attestation", str(attestation)]) == 0


def test_cli_refuses_a_mutable_manifest_pin(tmp_path):
    attestation = tmp_path / "image-attestation.json"
    attestation.write_text(json.dumps(verified()), encoding="utf-8")
    assert main(["--manifest", str(manifest(tmp_path, sha="trunk")), "--attestation", str(attestation)]) == 1


def test_gp_outputs_job_binds_provenance_to_the_candidate_source_with_read_only_permissions():
    workflow = yaml.safe_load((HERE.parent / ".github/workflows/dr-drill-local-docker.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["gp-outputs"]
    # Job-level, least privilege: attestation reads for `gh attestation verify`; no OIDC, no writes.
    assert job["permissions"] == {"contents": "read", "packages": "read", "attestations": "read"}
    steps = job["steps"]
    verify_index = next(i for i, step in enumerate(steps) if "gh attestation verify" in step.get("run", ""))
    verify = steps[verify_index]
    command = " ".join(verify["run"].replace("\\\n", " ").split())
    assert '"oci://$SERVER_IMAGE"' in command
    assert "--signer-workflow honua-io/honua-server/.github/workflows/nightly-container-build.yml" in command
    # Exactly two refs are tried, each pinned exactly: trunk, then the candidate tag for the
    # manifest sha's first 7 characters (honua-server#5791). No regex, no unpinned attempt.
    assert 'candidate_tag_ref="refs/tags/nightly-candidate-${SOURCE_SHA:0:7}"' in command
    assert 'for source_ref in refs/heads/trunk "$candidate_tag_ref"; do' in command
    assert '--source-ref "$source_ref" --source-digest "$SOURCE_SHA"' in command
    assert command.count("gh attestation verify") == 1
    assert "--cert-identity-regex" not in command
    assert 'if [ -z "$verified_ref" ]; then' in command and "exit 1" in command
    assert '--source-digest "$SOURCE_SHA"' in command
    assert verify["env"]["SOURCE_SHA"] == "${{ steps.candidate.outputs.source_sha }}"
    assert verify["env"]["SERVER_IMAGE"] == "${{ steps.candidate.outputs.server_image }}"
    assert "python tools/check_image_attestation.py --manifest artifacts/gp-candidate/platform-manifest.yaml" in command
    assert command.index("gh attestation verify") < command.index("check_image_attestation.py")
    assert not verify.get("continue-on-error")
    build_index = next(i for i, step in enumerate(steps) if step.get("id") == "worker")
    assert verify_index < build_index
