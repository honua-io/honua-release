"""#236: the publication-tag signing producer and its trust policy, proved on real signatures.

These are not mocks. Each test builds a throwaway git repository, generates an ephemeral SSH
key, and makes git produce a genuine annotated signed tag object. The expected fingerprint is
computed independently with `ssh-keygen -lf` rather than read back from the tool, and the
tamper tests mutate real signed bytes. Keys exist only inside pytest's tmp_path and are never
written into the repository.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

import tag_signing
from tag_signing import (CONTROL_POLICY, SigningError, check_namespaces, load_policy,
                         parse_verification, sign_tag, verify_tag)

REPOSITORY = "honua-release"
TAG = "honua-2026.1.0-rc.1"

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None,
                                reason="ssh-keygen is required to produce a real signed tag")


def run(*args: str, cwd: Path) -> str:
    result = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


@pytest.fixture
def signer(tmp_path):
    """An ephemeral SSH signing key plus its independently computed fingerprint."""
    key = tmp_path / "publication-key"
    run("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "release@example.test",
        cwd=tmp_path)
    listing = run("ssh-keygen", "-lf", str(key.with_suffix(".pub")), cwd=tmp_path)
    fingerprint = listing.split()[1]
    allowed = tmp_path / "allowed_signers"
    allowed.write_text(f'release@example.test namespaces="git" '
                       f'{key.with_suffix(".pub").read_text().strip()}\n', encoding="utf-8")
    return {"key": key, "public": key.with_suffix(".pub"), "fingerprint": fingerprint,
            "allowed": allowed}


@pytest.fixture
def repo(tmp_path, signer):
    path = tmp_path / "repo"
    path.mkdir()
    run("git", "init", "-q", "-b", "trunk", ".", cwd=path)
    for key, value in (("user.name", "Release Owner"), ("user.email", "release@example.test"),
                       ("gpg.format", "ssh"), ("user.signingkey", str(signer["public"])),
                       ("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        run("git", "config", key, value, cwd=path)
    (path / "README.md").write_text("candidate\n", encoding="utf-8")
    run("git", "add", "README.md", cwd=path)
    run("git", "commit", "-q", "-m", "candidate", cwd=path)
    return path


def policy(signer, *, fingerprint=None, signers=("release-owner",)):
    return {
        "schema_version": 1,
        "issue": "honua-io/honua-release#236",
        "signers": [{"id": "release-owner", "format": "ssh",
                     "fingerprint": signer["fingerprint"] if fingerprint is None else fingerprint}],
        "repositories": {REPOSITORY: {"tag_refs": ["refs/tags/honua-2026.1.*"],
                                      "signers": list(signers)}},
    }


def head(repo: Path) -> str:
    return run("git", "rev-parse", "HEAD", cwd=repo)


# --- the committed trust policy ---------------------------------------------------------------

def test_committed_policy_is_valid_and_nominates_no_signer():
    """Honest state of #236: the machinery is complete, the trust anchor is an operator input."""
    committed = load_policy()
    assert committed["signers"] == []
    assert check_namespaces(committed) == []


def test_committed_policy_covers_exactly_the_protected_tag_namespaces():
    controls = json.loads(CONTROL_POLICY.read_text(encoding="utf-8"))["repositories"]
    protected = {name: sorted(row["tag_refs"]) for name, row in controls.items() if row.get("tag_refs")}
    committed = load_policy()
    assert {name: sorted(row["tag_refs"]) for name, row in committed["repositories"].items()} == protected
    assert protected  # a policy covering nothing would pass vacuously


def test_namespace_drift_from_the_control_policy_is_rejected(tmp_path):
    committed = load_policy()
    drifted = copy.deepcopy(committed)
    drifted["repositories"]["honua-release"]["tag_refs"] = ["refs/tags/*"]
    assert any("differ from protected" in error for error in check_namespaces(drifted))
    del drifted["repositories"]["honua-helm"]
    assert any("no signing trust entry" in error for error in check_namespaces(drifted))


def test_policy_without_a_nominated_signer_cannot_sign_or_verify(repo, signer):
    empty = policy(signer, signers=())
    with pytest.raises(SigningError, match="no publication signing key is nominated"):
        sign_tag(repo, TAG, head(repo), "cut", empty, REPOSITORY, signer["allowed"])


@pytest.mark.parametrize("fingerprint", ["not-a-fingerprint", "SHA256:short", ""])
def test_policy_rejects_a_malformed_fingerprint(tmp_path, signer, fingerprint):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy(signer, fingerprint=fingerprint)), encoding="utf-8")
    with pytest.raises(SigningError, match="fingerprint"):
        load_policy(path)


# --- producing a real signed tag ---------------------------------------------------------------

def test_produces_a_signed_annotated_tag_and_a_bound_receipt(repo, signer):
    target = head(repo)
    receipt = sign_tag(repo, TAG, target, "2026.1.0-rc.1", policy(signer), REPOSITORY,
                       signer["allowed"])
    # Independently: git must now hold a tag *object*, and it must carry a signature block.
    assert run("git", "cat-file", "-t", TAG, cwd=repo) == "tag"
    assert "BEGIN SSH SIGNATURE" in run("git", "cat-file", "tag", TAG, cwd=repo)
    assert receipt["schema"] == "honua.signed-tag-receipt/v1"
    assert receipt["target"] == target and receipt["targetType"] == "commit"
    assert receipt["tagObject"] == run("git", "rev-parse", TAG, cwd=repo) != target
    assert receipt["signature"]["fingerprint"] == signer["fingerprint"]
    assert receipt["signature"]["signerId"] == "release-owner"


def test_refuses_a_tag_outside_the_protected_namespace(repo, signer):
    with pytest.raises(SigningError, match="outside"):
        sign_tag(repo, "v9.9.9", head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])
    assert run("git", "tag", "-l", cwd=repo) == ""


def test_refuses_to_move_an_existing_publication_tag(repo, signer):
    sign_tag(repo, TAG, head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])
    with pytest.raises(SigningError, match="already exists"):
        sign_tag(repo, TAG, head(repo), "recut", policy(signer), REPOSITORY, signer["allowed"])


def test_refuses_a_mutable_target(repo, signer):
    with pytest.raises(SigningError, match="immutable 40-character"):
        sign_tag(repo, TAG, "trunk", "cut", policy(signer), REPOSITORY, signer["allowed"])


def test_refuses_an_unauthorized_configured_key(repo, signer, tmp_path):
    other = tmp_path / "other-key"
    run("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other), "-C", "other@example.test",
        cwd=tmp_path)
    run("git", "config", "user.signingkey", str(other.with_suffix(".pub")), cwd=repo)
    with pytest.raises(SigningError, match="not authorized"):
        sign_tag(repo, TAG, head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])


# --- verifying what is actually there -----------------------------------------------------------

def test_lightweight_tag_is_not_a_signed_tag(repo, signer):
    """`gh release create` writes exactly this: a ref straight at a commit, with no tag object."""
    run("git", "tag", TAG, cwd=repo)
    with pytest.raises(SigningError, match="lightweight tag carries no signature"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, signer["allowed"])


def test_unsigned_annotated_tag_is_rejected(repo, signer):
    run("git", "tag", "-a", "-m", "unsigned", TAG, cwd=repo)
    assert run("git", "cat-file", "-t", TAG, cwd=repo) == "tag"
    with pytest.raises(SigningError, match="did not verify"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, signer["allowed"])


def test_signature_by_an_unlisted_key_is_rejected(repo, signer, tmp_path):
    """A real, valid signature is still untrusted when the policy does not name its key."""
    other = tmp_path / "other-key"
    run("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other), "-C", "other@example.test",
        cwd=tmp_path)
    run("git", "config", "user.signingkey", str(other.with_suffix(".pub")), cwd=repo)
    run("git", "tag", "-s", "-a", "-m", "cut", TAG, cwd=repo)
    allowed = tmp_path / "allowed_other"
    allowed.write_text(f'other@example.test namespaces="git" '
                       f'{other.with_suffix(".pub").read_text().strip()}\n', encoding="utf-8")
    with pytest.raises(SigningError, match="does not authorize"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, allowed)


def test_allowed_signers_cannot_introduce_an_unpoliced_key(repo, signer, tmp_path):
    """The operator's trust material is itself constrained by the committed fingerprints."""
    other = tmp_path / "other-key"
    run("ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(other), "-C", "other@example.test",
        cwd=tmp_path)
    sign_tag(repo, TAG, head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])
    widened = tmp_path / "allowed_widened"
    widened.write_text(signer["allowed"].read_text(encoding="utf-8")
                       + f'other@example.test namespaces="git" '
                         f'{other.with_suffix(".pub").read_text().strip()}\n', encoding="utf-8")
    with pytest.raises(SigningError, match="does not authorize"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, widened)


def test_tampered_tag_message_breaks_the_signature(repo, signer):
    """Rewrite the signed payload while keeping the same signature block: verification must fail."""
    sign_tag(repo, TAG, head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])
    raw = run("git", "cat-file", "tag", TAG, cwd=repo)
    forged = raw.replace("\ncut\n", "\ncut (edited)\n", 1)
    assert forged != raw
    written = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tag", "-w", "--stdin"],
                             input=forged + "\n", capture_output=True, text=True)
    assert written.returncode == 0, written.stderr
    run("git", "update-ref", f"refs/tags/{TAG}", written.stdout.strip(), cwd=repo)
    with pytest.raises(SigningError, match="did not verify"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, signer["allowed"])


def test_verification_requires_the_operator_trust_material(repo, signer):
    sign_tag(repo, TAG, head(repo), "cut", policy(signer), REPOSITORY, signer["allowed"])
    with pytest.raises(SigningError, match="allowed-signers"):
        verify_tag(repo, TAG, policy(signer), REPOSITORY, None)


def test_openpgp_status_parsing_reads_the_fingerprint_not_the_verdict_text():
    good = "[GNUPG:] GOODSIG 1234\n[GNUPG:] VALIDSIG " + "A" * 40 + " 2026-09-07\n"
    assert parse_verification(good, "openpgp") == "A" * 40
    with pytest.raises(SigningError, match="did not verify"):
        parse_verification("[GNUPG:] BADSIG 1234\n", "openpgp")
    with pytest.raises(SigningError, match="did not verify"):
        parse_verification('Good "git" signature for x with ED25519 key SHA256:abc\n', "openpgp")


def test_cli_reports_the_blocked_trust_anchor(capsys, repo):
    assert tag_signing.main(["verify", REPOSITORY, TAG, "--git-dir", str(repo)]) == 1
    assert "no publication signing key is nominated" in capsys.readouterr().out


def test_cli_check_policy_passes_on_the_committed_policy(capsys):
    assert tag_signing.main(["check-policy"]) == 0
    assert "0 signer(s) nominated" in capsys.readouterr().out


# --- a receipt only qualifies against the policy that authorized the signer ----------------------

@pytest.fixture
def qualified(repo, signer, tmp_path):
    """A real signed tag plus the receipt, trust-policy file and audited release that back it."""
    path = tmp_path / "trust-policy.json"
    path.write_text(json.dumps(policy(signer), indent=2) + "\n", encoding="utf-8")
    target = head(repo)
    receipt = sign_tag(repo, TAG, target, "cut", policy(signer), REPOSITORY,
                       signer["allowed"], path)
    return {"receipt": receipt, "path": path, "repo": repo, "target": target,
            "tag_refs": ["refs/tags/honua-2026.1.*"],
            "release": {"tag": TAG, "target": target, "gitDir": str(repo),
                        "allowedSigners": str(signer["allowed"])}}


def qualify(qualified, receipt=None, **release):
    return tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], qualified["receipt"] if receipt is None else receipt,
        qualified["path"], release={**qualified["release"], **release} if release is not False
        else None)


def test_receipt_qualifies_against_its_own_trust_policy(qualified):
    assert tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"], qualified["receipt"],
                                       qualified["path"], qualified["release"]) is None


def test_receipt_does_not_qualify_against_the_committed_policy(qualified):
    """The committed policy nominates nobody, so a receipt from any other policy is not evidence."""
    reason = tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"], qualified["receipt"],
                                         release=qualified["release"])
    assert "different trust policy bytes" in reason


def test_editing_the_trust_policy_after_signing_invalidates_the_receipt(qualified):
    widened = json.loads(qualified["path"].read_text(encoding="utf-8"))
    widened["repositories"][REPOSITORY]["tag_refs"].append("refs/tags/*")
    qualified["path"].write_text(json.dumps(widened, indent=2) + "\n", encoding="utf-8")
    assert "different trust policy bytes" in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], qualified["receipt"], qualified["path"],
        qualified["release"])


@pytest.mark.parametrize("field,value,expected", [
    ("schema", "honua.signed-tag-receipt/v2", "expected a"),
    ("repository", "honua-helm", "not honua-release"),
    ("tag", "v9.9.9", "outside the protected"),
    ("target", "trunk", "immutable git object id"),
])
def test_receipt_fields_must_be_exact(qualified, field, value, expected):
    forged = copy.deepcopy(qualified["receipt"])
    forged[field] = value
    assert expected in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"], qualified["release"])


def test_receipt_claiming_a_lightweight_tag_is_rejected(qualified):
    forged = copy.deepcopy(qualified["receipt"])
    forged["tagObject"] = forged["target"]
    assert "lightweight tag" in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"], qualified["release"])


def test_receipt_with_an_unlisted_fingerprint_is_rejected(qualified):
    forged = copy.deepcopy(qualified["receipt"])
    forged["signature"] = {**forged["signature"], "fingerprint": "SHA256:" + "A" * 43}
    assert "not an authorized publication signer" in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"], qualified["release"])


# --- a receipt is a claim; qualification re-verifies the tag and binds it to the release -------

def test_a_forged_receipt_never_qualifies_without_a_repository_to_re_verify_in(qualified):
    """Copying the committed policy digest, an authorized fingerprint and two random object ids
    used to be enough. Nothing is signed here, and nothing is checked out."""
    forged = copy.deepcopy(qualified["receipt"])
    forged["tagObject"], forged["target"] = "a" * 40, "b" * 40
    assert "never qualifies a release on its own" in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"])
    assert "re-verify the signed tag object in" in tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"],
        {"tag": TAG, "target": "b" * 40})


def test_a_receipt_for_a_tag_that_does_not_exist_does_not_re_verify(qualified):
    """A whole-cloth receipt for a tag nobody ever created: no object, so no signature."""
    invented = "honua-2026.1.9-rc.9"
    forged = copy.deepcopy(qualified["receipt"])
    forged["tag"], forged["tagObject"], forged["target"] = invented, "a" * 40, "b" * 40
    reason = tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], forged, qualified["path"],
        {**qualified["release"], "tag": invented, "target": "b" * 40})
    assert "did not re-verify in the audited repository" in reason


def test_a_receipt_claiming_a_different_object_than_the_signed_tag_is_refused(qualified):
    """The tag really is signed, but the receipt names some other tag object."""
    forged = copy.deepcopy(qualified["receipt"])
    forged["tagObject"] = "a" * 40
    reason = tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"], forged,
                                         qualified["path"], qualified["release"])
    assert "tagObject" in reason and "disagrees with the tag re-verified" in reason


def test_a_genuine_receipt_for_another_tag_does_not_qualify_this_release(repo, signer, qualified):
    """A throwaway signed tag inside the namespace must not clear the control for a release
    whose own tag is unsigned."""
    other = "honua-2026.1.0-rc.0"
    receipt = sign_tag(repo, other, head(repo), "throwaway", policy(signer), REPOSITORY,
                       signer["allowed"], qualified["path"])
    reason = tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"], receipt,
                                         qualified["path"], qualified["release"])
    assert f"not the audited publication tag {TAG!r}" in reason


def test_a_receipt_for_a_different_candidate_revision_does_not_qualify(repo, signer, qualified):
    run("git", "commit", "-q", "--allow-empty", "-m", "later", cwd=repo)
    reason = tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], qualified["receipt"], qualified["path"],
        {**qualified["release"], "target": head(repo)})
    assert "is not the audited candidate revision" in reason


def test_re_verification_still_needs_the_operator_trust_material(qualified):
    reason = tag_signing.qualify_receipt(
        REPOSITORY, qualified["tag_refs"], qualified["receipt"], qualified["path"],
        {**qualified["release"], "allowedSigners": None})
    assert "allowed-signers" in reason


def test_the_audited_release_identity_must_be_exact(qualified):
    for release in ({"target": qualified["target"]}, {"tag": TAG},
                    {"tag": TAG, "target": "refs/heads/trunk"}):
        assert "exact publication tag and candidate revision" in tag_signing.qualify_receipt(
            REPOSITORY, qualified["tag_refs"], qualified["receipt"], qualified["path"], release)


def test_controls_audit_stays_red_while_no_signer_is_nominated(qualified):
    from release_controls import audit_repository
    row = {'release_refs': ['refs/heads/release/2026.1'], 'required_checks': ['validate'],
           'tag_refs': ['refs/tags/honua-2026.1.*'],
           'code_owners': ['mikemcdougall', 'independent-reviewer']}
    signing = {'receipt': qualified["receipt"], 'gitDir': str(qualified["repo"]),
               'releaseTag': TAG, 'releaseTarget': qualified["target"]}
    errors = audit_repository(row, {'rulesets': []}, signing, REPOSITORY)
    assert any('native signed-tag producer and trusted verification not qualified' in error
               for error in errors)
    assert any('different trust policy bytes' in error for error in errors)


def test_controls_audit_refuses_a_bare_receipt_with_no_release_binding(qualified):
    """The receipts file used to be repository -> receipt, which bound nothing."""
    from release_controls import audit_repository
    row = {'release_refs': ['refs/heads/release/2026.1'], 'required_checks': ['validate'],
           'tag_refs': ['refs/tags/honua-2026.1.*'],
           'code_owners': ['mikemcdougall', 'independent-reviewer']}
    errors = audit_repository(row, {'rulesets': []}, qualified["receipt"], REPOSITORY)
    assert any(error == 'native signed-tag producer and trusted verification not qualified'
               for error in errors)


def test_controls_audit_reports_signing_namespace_drift(tmp_path):
    """check_namespaces is part of the audit, not an optional operator command."""
    from release_controls import POLICY, audit
    control = json.loads(POLICY.read_text(encoding="utf-8"))
    drifted = json.loads(tag_signing.TRUST_POLICY.read_text(encoding="utf-8"))
    drifted["repositories"]["honua-release"]["tag_refs"] = ["refs/tags/*"]
    path = tmp_path / "tag-signing-policy.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")
    result = audit(control, {'repositories': {}}, None, POLICY, path)
    assert any("signing namespaces" in message
               for message in result["signing_namespace_parity"])
    assert result["status"] == "fail"


def test_committed_policies_are_in_namespace_parity_so_the_audit_is_unchanged():
    from release_controls import POLICY, audit
    control = json.loads(POLICY.read_text(encoding="utf-8"))
    result = audit(control, {'repositories': {}})
    assert "signing_namespace_parity" not in result
