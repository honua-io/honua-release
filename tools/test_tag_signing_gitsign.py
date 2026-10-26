"""#236 / founder decision D11: keyless (gitsign) publication tags, proved on a real signature.

honua-release publication tags are signed by promote.yml's GitHub Actions OIDC identity with
gitsign, and promotion refuses any published tag that `gitsign verify-tag` cannot tie to that
exact identity and the issuer https://token.actions.githubusercontent.com.

No signature here is mocked. A live signature by Honua's own identity can only be minted inside
promote.yml, so the fixture is a genuine, Rekor-logged gitsign tag signature made by a different
GitHub Actions workflow (tools/fixtures/gitsign/README.md). That one real signature proves both
directions: it verifies when the policy names its true identity, and it is refused when the
policy names Honua's promote identity. Unsigned and lightweight tags are produced locally.

Verification talks to the public Sigstore infrastructure (TUF root, Rekor). Without gitsign on
PATH the module skips locally; on CI (`CI=true`) a missing gitsign is a failure, not a skip.
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

import tag_signing
from tag_signing import SigningError, load_policy, verify_gitsign, verify_tag

ROOT = Path(__file__).resolve().parents[1]
PACK = Path(__file__).with_name("fixtures") / "gitsign" / "foreign-workflow-signed-tag.pack"
REPOSITORY = "honua-release"
ISSUER = "https://token.actions.githubusercontent.com"
PROMOTE_IDENTITY = ("https://github.com/honua-io/honua-release/.github/workflows/promote.yml"
                    "@refs/heads/trunk")
FOREIGN_IDENTITY = "https://github.com/ozeranskii/httptap/.github/workflows/release.yml@refs/heads/main"
FOREIGN_TAG = "v0.8.2"
FOREIGN_TAG_OBJECT = "8cf0333e2c43016cb5fe064c182f005e9f5f26f6"
FOREIGN_TARGET = "db70f65248fb342258f693054138fa2fb353b3cb"
FOREIGN_REKOR_INDEX = "3244071886"
PUBLICATION_TAG = "honua-2026.1.0"

GITSIGN = os.environ.get("GITSIGN") or shutil.which("gitsign")
if GITSIGN is None and os.environ.get("CI") == "true":
    raise RuntimeError("gitsign must be installed on CI (tools/install_gitsign.sh); "
                       "the keyless tag verifier cannot be skipped there")
needs_gitsign = pytest.mark.skipif(GITSIGN is None, reason="gitsign is not installed")


def run(*args: str, cwd: Path, **kwargs) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True, **kwargs)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def keyless_policy(identity: str = PROMOTE_IDENTITY, tag_refs=("refs/tags/honua-2026.1.*",),
                   issuer: str = ISSUER) -> dict:
    return {
        "schema_version": 1, "issue": "honua-io/honua-release#236",
        "signers": [{"id": "publication-workflow", "format": "gitsign",
                     "issuer": issuer, "identity": identity}],
        "repositories": {REPOSITORY: {"tag_refs": list(tag_refs),
                                      "signers": ["publication-workflow"]}},
    }


def load_fixture(path: Path, *, bare: bool = False) -> Path:
    """A repository holding the real foreign-signed tag, loaded from the committed pack."""
    run("git", "init", "-q", *(["--bare"] if bare else []), str(path), cwd=path.parent)
    git_dir = path if bare else path / ".git"
    with PACK.open("rb") as pack:
        indexed = subprocess.run(["git", "--git-dir", str(git_dir), "index-pack", "--stdin"],
                                 stdin=pack, capture_output=True)
    assert indexed.returncode == 0, indexed.stderr
    run("git", "--git-dir", str(git_dir), "update-ref", f"refs/tags/{FOREIGN_TAG}",
        FOREIGN_TAG_OBJECT, cwd=path)
    return path


@pytest.fixture
def foreign(tmp_path) -> Path:
    return load_fixture(tmp_path / "foreign")


@pytest.fixture
def candidate(tmp_path) -> Path:
    path = tmp_path / "candidate"
    path.mkdir()
    run("git", "init", "-q", "-b", "trunk", ".", cwd=path)
    for key, value in (("user.name", "Release Owner"), ("user.email", "release@example.test"),
                       ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        run("git", "config", key, value, cwd=path)
    (path / "README.md").write_text("candidate\n", encoding="utf-8")
    run("git", "add", "README.md", cwd=path)
    run("git", "commit", "-q", "-m", "candidate", cwd=path)
    return path


# --- the committed trust policy ---------------------------------------------------------------

def test_committed_policy_binds_publication_tags_to_the_promote_workflow_identity():
    committed = load_policy()
    signers = {signer["id"]: signer for signer in committed["signers"]}
    release = committed["repositories"][REPOSITORY]
    assert release["signers"] == ["honua-release-promote"]
    assert signers["honua-release-promote"] == {
        "id": "honua-release-promote", "format": "gitsign",
        "issuer": ISSUER, "identity": PROMOTE_IDENTITY}
    # The keyless identity is honua-release's alone; the other native-tag repositories still
    # nominate nobody and stay fail-closed.
    for name, entry in committed["repositories"].items():
        if name != REPOSITORY:
            assert entry["signers"] == [], name


def test_fixture_pack_holds_exactly_the_documented_signed_tag(foreign):
    assert run("git", "cat-file", "-t", FOREIGN_TAG, cwd=foreign) == "tag"
    assert run("git", "rev-parse", FOREIGN_TAG, cwd=foreign) == FOREIGN_TAG_OBJECT
    assert run("git", "rev-parse", f"{FOREIGN_TAG}^{{}}", cwd=foreign) == FOREIGN_TARGET
    assert "-----BEGIN SIGNED MESSAGE-----" in run("git", "cat-file", "tag", FOREIGN_TAG, cwd=foreign)


@pytest.mark.parametrize("change,expected", [
    ({"issuer": "https://oauth2.sigstore.dev/auth"}, "must name issuer"),
    ({"issuer": None}, "must name issuer"),
    ({"identity": "https://github.com/honua-io/honua-release/.github/workflows/promote.yml@.*"},
     "exact GitHub workflow identity"),
    ({"identity": "mike@honua.io"}, "exact GitHub workflow identity"),
    ({"fingerprint": "SHA256:" + "A" * 43}, "keyless and names no fingerprint"),
])
def test_policy_rejects_a_loose_keyless_signer(tmp_path, change, expected):
    policy = keyless_policy()
    policy["signers"][0].update(change)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    with pytest.raises(SigningError, match=expected):
        load_policy(path)


# --- green: a genuine signature verifies against its own identity -----------------------------

@needs_gitsign
def test_genuine_signature_verifies_for_the_identity_that_made_it(foreign, tmp_path):
    path = tmp_path / "trust.json"
    policy = keyless_policy(FOREIGN_IDENTITY, ["refs/tags/v*"])
    path.write_text(json.dumps(policy), encoding="utf-8")
    receipt = verify_tag(foreign, FOREIGN_TAG, policy, REPOSITORY, policy_path=path)
    assert receipt["tagObject"] == FOREIGN_TAG_OBJECT
    assert receipt["target"] == FOREIGN_TARGET and receipt["targetType"] == "commit"
    assert receipt["signature"] == {"format": "gitsign", "identity": FOREIGN_IDENTITY,
                                    "issuer": ISSUER, "signerId": "publication-workflow",
                                    "rekorLogIndex": FOREIGN_REKOR_INDEX}


# --- red: everything else refuses -------------------------------------------------------------

@needs_gitsign
def test_valid_signature_by_another_workflow_identity_is_refused(foreign):
    """A real, transparency-logged signature is not a Honua publication signature."""
    policy = keyless_policy(PROMOTE_IDENTITY, ["refs/tags/v*"])
    with pytest.raises(SigningError, match="not from an authorized publication identity") as error:
        verify_tag(foreign, FOREIGN_TAG, policy, REPOSITORY)
    assert PROMOTE_IDENTITY in str(error.value)


@needs_gitsign
def test_right_identity_from_the_wrong_issuer_is_refused(foreign):
    signer = {"id": "x", "format": "gitsign", "identity": FOREIGN_IDENTITY,
              "issuer": "https://oauth2.sigstore.dev/auth"}
    with pytest.raises(SigningError, match="not from an authorized publication identity"):
        verify_gitsign(foreign, FOREIGN_TAG, [signer])


@needs_gitsign
def test_tampered_signed_payload_is_refused(foreign):
    raw = run("git", "cat-file", "tag", FOREIGN_TAG, cwd=foreign)
    forged = raw.replace("Release v0.8.2", "Release v0.8.3", 1)
    assert forged != raw
    written = run("git", "hash-object", "-t", "tag", "-w", "--stdin", cwd=foreign, input=forged + "\n")
    run("git", "update-ref", f"refs/tags/{FOREIGN_TAG}", written, cwd=foreign)
    with pytest.raises(SigningError, match="not from an authorized publication identity"):
        verify_tag(foreign, FOREIGN_TAG, keyless_policy(FOREIGN_IDENTITY, ["refs/tags/v*"]), REPOSITORY)


def test_unsigned_annotated_tag_is_refused(candidate):
    run("git", "tag", "-a", "-m", "unsigned", PUBLICATION_TAG, cwd=candidate)
    with pytest.raises(SigningError, match="unsigned annotated tag"):
        verify_tag(candidate, PUBLICATION_TAG, keyless_policy(), REPOSITORY)


def test_lightweight_tag_is_refused(candidate):
    run("git", "tag", PUBLICATION_TAG, cwd=candidate)
    with pytest.raises(SigningError, match="lightweight tag carries no signature"):
        verify_tag(candidate, PUBLICATION_TAG, keyless_policy(), REPOSITORY)


def test_missing_gitsign_fails_closed(foreign, monkeypatch):
    monkeypatch.setenv("GITSIGN", "")
    monkeypatch.setattr(tag_signing.shutil, "which", lambda name: None)
    with pytest.raises(SigningError, match="gitsign is required"):
        verify_tag(foreign, FOREIGN_TAG, keyless_policy(FOREIGN_IDENTITY, ["refs/tags/v*"]), REPOSITORY)


@needs_gitsign
def test_receipt_qualification_rechecks_the_keyless_identity(foreign, tmp_path):
    path = tmp_path / "trust.json"
    policy = keyless_policy(FOREIGN_IDENTITY, ["refs/tags/v*"])
    path.write_text(json.dumps(policy), encoding="utf-8")
    receipt = verify_tag(foreign, FOREIGN_TAG, policy, REPOSITORY, policy_path=path)
    expected = {"tag": FOREIGN_TAG, "target": FOREIGN_TARGET}
    source = {"gitDir": str(foreign)}
    assert tag_signing.qualify_receipt(REPOSITORY, ["refs/tags/v*"], receipt, path,
                                       expected=expected, source=source) is None
    forged = copy.deepcopy(receipt)
    forged["signature"]["identity"] = PROMOTE_IDENTITY
    assert "receipt identity is not an authorized" in tag_signing.qualify_receipt(
        REPOSITORY, ["refs/tags/v*"], forged, path, expected=expected, source=source)


# --- the actual promote.yml guard step, executed ----------------------------------------------

@pytest.fixture
def remote(tmp_path) -> Path:
    """A served publication remote that already holds the foreign-signed tag's objects."""
    path = load_fixture(tmp_path / "published.git", bare=True)
    run("git", "config", "uploadpack.allowFilter", "true", cwd=path)
    return path


def run_guard(tmp_path, remote, tag, target, *, trust_identity=PROMOTE_IDENTITY, tag_refs=None):
    steps = yaml.safe_load((ROOT / ".github/workflows/promote.yml").read_text())["jobs"]["promote"]["steps"]
    preserve = next(s for s in steps if s.get("name") == "Preserve current publication trust policy and verifier")
    guard = next(s for s in steps if "verify-remote" in s.get("run", ""))
    runner = tmp_path / "runner"
    runner.mkdir()
    env = {**os.environ, "RUNNER_TEMP": str(runner), "GITHUB_OUTPUT": str(tmp_path / "out"),
           "PUBLICATION_TAG": tag, "CERTIFIED_SHA": target,
           "PUBLICATION_REMOTE": f"file://{remote}"}
    subprocess.run(["bash", "-c", preserve["run"]], cwd=ROOT, env=env, check=True, capture_output=True)
    controls = runner / "publication-trust/certification/release-controls"
    trust = json.loads((controls / "tag-signing-policy.json").read_text())
    assert trust["signers"][0]["identity"] == PROMOTE_IDENTITY  # the preserved policy is the real one
    trust["signers"][0]["identity"] = trust_identity
    if tag_refs:
        trust["repositories"][REPOSITORY]["tag_refs"] = tag_refs
        control = json.loads((controls / "policy.json").read_text())
        control["repositories"][REPOSITORY]["tag_refs"] = tag_refs
        (controls / "policy.json").write_text(json.dumps(control), encoding="utf-8")
    (controls / "tag-signing-policy.json").write_text(json.dumps(trust), encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    command = guard["run"].replace('python "$RUNNER_TEMP', f'"{sys.executable}" "$RUNNER_TEMP')
    result = subprocess.run(["bash", "-c", command], cwd=work, env=env, text=True, capture_output=True)
    receipt = work / "signed-tag-receipt.json"
    return result, (json.loads(receipt.read_text()) if receipt.exists() else None)


@needs_gitsign
def test_promotion_guard_refuses_a_published_tag_signed_by_another_identity(tmp_path, remote):
    # Republish the genuine foreign signature under Honua's publication tag name.
    run("git", "update-ref", f"refs/tags/{PUBLICATION_TAG}", FOREIGN_TAG_OBJECT, cwd=remote)
    result, receipt = run_guard(tmp_path, remote, PUBLICATION_TAG, FOREIGN_TARGET)
    assert result.returncode != 0
    assert "not from an authorized publication identity" in result.stdout + result.stderr
    assert receipt is None


@needs_gitsign
@pytest.mark.parametrize("kind", ["unsigned", "lightweight"])
def test_promotion_guard_refuses_an_unsigned_published_tag(tmp_path, remote, candidate, kind):
    target = run("git", "rev-parse", "HEAD", cwd=candidate)
    if kind == "unsigned":
        run("git", "tag", "-a", "-m", "unsigned", PUBLICATION_TAG, cwd=candidate)
    else:
        run("git", "tag", PUBLICATION_TAG, cwd=candidate)
    run("git", "push", "-q", str(remote), f"refs/tags/{PUBLICATION_TAG}", cwd=candidate)
    result, receipt = run_guard(tmp_path, remote, PUBLICATION_TAG, target)
    assert result.returncode != 0
    expected = "unsigned annotated tag" if kind == "unsigned" else "not an annotated tag object"
    assert expected in result.stdout + result.stderr
    assert receipt is None


@needs_gitsign
@pytest.mark.parametrize("target,ok", [(FOREIGN_TARGET, True), ("a" * 40, False)])
def test_promotion_guard_accepts_only_the_trusted_identity_at_the_certified_commit(
        tmp_path, remote, target, ok):
    """Green path of the real step: same bytes, policy naming the identity that signed them."""
    result, receipt = run_guard(tmp_path, remote, FOREIGN_TAG, target,
                                trust_identity=FOREIGN_IDENTITY, tag_refs=["refs/tags/v*"])
    if ok:
        assert result.returncode == 0, result.stdout + result.stderr
        assert receipt["tagObject"] == FOREIGN_TAG_OBJECT
        assert receipt["target"] == FOREIGN_TARGET
        assert receipt["signature"]["identity"] == FOREIGN_IDENTITY
        assert receipt["signature"]["issuer"] == ISSUER
    else:
        assert result.returncode != 0
        assert "exact certified candidate" in result.stdout + result.stderr
        assert receipt is None
