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
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

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
    """A real signed tag plus the trust policy, release identity and repository that qualify it."""
    path = tmp_path / "trust-policy.json"
    path.write_text(json.dumps(policy(signer), indent=2) + "\n", encoding="utf-8")
    target = head(repo)
    receipt = sign_tag(repo, TAG, target, "cut", policy(signer), REPOSITORY,
                       signer["allowed"], path)
    return {"receipt": receipt, "path": path, "tag_refs": ["refs/tags/honua-2026.1.*"],
            "expected": {"tag": TAG, "target": target},
            "source": {"gitDir": str(repo), "allowedSigners": str(signer["allowed"])}}


def check(qualified, receipt=None, **overrides):
    kwargs = {"expected": qualified["expected"], "source": qualified["source"], **overrides}
    return tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"],
                                       receipt or qualified["receipt"], qualified["path"], **kwargs)


def test_receipt_qualifies_against_its_own_trust_policy(qualified):
    assert check(qualified) is None


def test_receipt_does_not_qualify_against_the_committed_policy(qualified):
    """The committed policy nominates nobody, so a receipt from any other policy is not evidence."""
    reason = tag_signing.qualify_receipt(REPOSITORY, qualified["tag_refs"], qualified["receipt"],
                                         expected=qualified["expected"],
                                         source=qualified["source"])
    assert "different trust policy bytes" in reason


def test_editing_the_trust_policy_after_signing_invalidates_the_receipt(qualified):
    widened = json.loads(qualified["path"].read_text(encoding="utf-8"))
    widened["repositories"][REPOSITORY]["tag_refs"].append("refs/tags/*")
    qualified["path"].write_text(json.dumps(widened, indent=2) + "\n", encoding="utf-8")
    assert "different trust policy bytes" in check(qualified)


@pytest.mark.parametrize("field,value,expected", [
    ("schema", "honua.signed-tag-receipt/v2", "expected a"),
    ("repository", "honua-helm", "not honua-release"),
    ("tag", "v9.9.9", "outside the protected"),
    ("target", "trunk", "immutable git object id"),
])
def test_receipt_fields_must_be_exact(qualified, field, value, expected):
    forged = copy.deepcopy(qualified["receipt"])
    forged[field] = value
    assert expected in check(qualified, forged)


def test_receipt_claiming_a_lightweight_tag_is_rejected(qualified):
    forged = copy.deepcopy(qualified["receipt"])
    forged["tagObject"] = forged["target"]
    assert "lightweight tag" in check(qualified, forged)


def test_receipt_with_an_unlisted_fingerprint_is_rejected(qualified):
    forged = copy.deepcopy(qualified["receipt"])
    forged["signature"] = {**forged["signature"], "fingerprint": "SHA256:" + "A" * 43}
    assert "not an authorized publication signer" in check(qualified, forged)


def test_controls_audit_stays_red_while_no_signer_is_nominated(qualified):
    from release_controls import audit_repository
    row = {'release_refs': ['refs/heads/release/2026.1'], 'required_checks': ['validate'],
           'tag_refs': ['refs/tags/honua-2026.1.*'],
           'code_owners': ['mikemcdougall', 'independent-reviewer']}
    errors = audit_repository(row, {'rulesets': []},
                              {'receipt': qualified["receipt"], **qualified["source"]},
                              REPOSITORY, qualified["expected"])
    assert any('native signed-tag producer and trusted verification not qualified' in error
               for error in errors)
    assert any('different trust policy bytes' in error for error in errors)


# --- review hardening: a receipt file is a claim, not evidence ----------------------------------

def forged_receipt(qualified, **overrides):
    """What an attacker with write access to the receipts file can trivially author."""
    value = {"schema": "honua.signed-tag-receipt/v1", "issue": "honua-io/honua-release#236",
             "repository": REPOSITORY, "tag": TAG,
             "tagObject": "a" * 40, "target": "b" * 40, "targetType": "commit",
             "signature": copy.deepcopy(qualified["receipt"]["signature"]),
             "policySha256": qualified["receipt"]["policySha256"],
             "verifiedAt": "2026-09-07T00:00:00Z"}
    value.update(overrides)
    return value


def test_a_hand_written_receipt_cannot_clear_the_signing_control(qualified):
    """Copying the policy digest and an authorized fingerprint must not be enough."""
    forged = forged_receipt(qualified)
    expected = {"tag": TAG, "target": forged["target"]}
    reason = check(qualified, forged, expected=expected)
    assert "disagrees with the tag object that actually verified" in reason


def test_a_hand_written_receipt_for_a_tag_that_does_not_exist_is_rejected(qualified, tmp_path):
    """No signed tag anywhere: the receipt alone must not qualify anything."""
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    run("git", "init", "-q", "-b", "trunk", ".", cwd=empty)
    forged = forged_receipt(qualified)
    reason = check(qualified, forged, expected={"tag": TAG, "target": forged["target"]},
                   source={"gitDir": str(empty), "allowedSigners": qualified["source"]["allowedSigners"]})
    assert "re-verification of the signed tag failed" in reason


def test_qualification_requires_a_repository_to_re_verify_in(qualified):
    assert "unauthenticated" in check(qualified, source=None)
    assert "unauthenticated" in check(qualified, source={"allowedSigners": "x"})


def test_re_verification_uses_the_real_tag_not_the_receipt(repo, signer, qualified):
    """Tamper the signed tag after the receipt was written: qualification must notice."""
    raw = run("git", "cat-file", "tag", TAG, cwd=repo)
    forged = raw.replace("\ncut\n", "\ncut (edited)\n", 1)
    written = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tag", "-w", "--stdin"],
                             input=forged + "\n", capture_output=True, text=True)
    assert written.returncode == 0, written.stderr
    run("git", "update-ref", f"refs/tags/{TAG}", written.stdout.strip(), cwd=repo)
    assert "re-verification of the signed tag failed" in check(qualified)


def test_a_receipt_for_another_signed_tag_cannot_clear_the_audited_release(repo, signer, qualified):
    """A genuine receipt for an old tag must not qualify the release currently being audited."""
    other = "honua-2026.1.0-rc.9"
    receipt = sign_tag(repo, other, head(repo), "older cut", policy(signer), REPOSITORY,
                       signer["allowed"], qualified["path"])
    reason = check(qualified, receipt)
    assert "not the audited release tag" in reason


def test_a_receipt_for_another_candidate_revision_is_rejected(qualified):
    assert "not the audited candidate revision" in check(
        qualified, expected={"tag": TAG, "target": "c" * 40})


@pytest.mark.parametrize("identity", [None, {}, {"tag": TAG}, {"target": "c" * 40},
                                      {"tag": TAG, "target": "trunk"}])
def test_qualification_needs_an_exact_release_identity(qualified, identity):
    assert "no audited release identity" in check(qualified, expected=identity)


def test_audit_reports_signing_namespace_drift(tmp_path, monkeypatch):
    """Parity is checked by the audit itself, not only by the standalone check-policy command."""
    import release_controls
    drifted = json.loads(tag_signing.TRUST_POLICY.read_text(encoding="utf-8"))
    drifted["repositories"]["honua-release"]["tag_refs"] = ["refs/tags/*"]
    path = tmp_path / "drifted-policy.json"
    path.write_text(json.dumps(drifted), encoding="utf-8")
    monkeypatch.setattr(tag_signing, "TRUST_POLICY", path)
    row = {'release_refs': ['refs/heads/release/2026.1'], 'required_checks': ['validate'],
           'tag_refs': ['refs/tags/honua-2026.1.*'], 'code_owners': ['mikemcdougall']}
    errors = release_controls.audit_repository(row, {'rulesets': []}, None, 'honua-release')
    assert any('signing namespaces' in error and 'differ from protected' in error
               for error in errors)


def test_audit_treats_an_unreadable_signing_policy_as_failure(tmp_path, monkeypatch):
    import release_controls
    missing = tmp_path / "absent-policy.json"
    monkeypatch.setattr(tag_signing, "TRUST_POLICY", missing)
    row = {'release_refs': ['refs/heads/release/2026.1'], 'required_checks': ['validate'],
           'tag_refs': ['refs/tags/honua-2026.1.*'], 'code_owners': ['mikemcdougall']}
    errors = release_controls.audit_repository(row, {'rulesets': []}, None, 'honua-release')
    assert any('signing trust policy unreadable' in error for error in errors)


@pytest.fixture
def publication_remote(tmp_path, repo):
    remote = tmp_path / 'published.git'
    run('git', 'init', '--bare', '-q', str(remote), cwd=tmp_path)
    run('git', 'push', str(remote), 'HEAD:refs/heads/trunk', cwd=repo)
    return remote


def publish_signed(repo, signer, publication_remote, tag=TAG):
    # Produce the fixture independently of sign_tag/verify_remote_tag.
    run('git', 'tag', '-s', '-m', 'first release', tag, head(repo), cwd=repo)
    run('git', 'push', str(publication_remote), f'refs/tags/{tag}', cwd=repo)
    return run('git', 'rev-parse', f'refs/tags/{tag}', cwd=repo)


def test_remote_verification_proves_published_tag_bytes(repo, signer, publication_remote):
    target = head(repo)
    tag_object = publish_signed(repo, signer, publication_remote)
    receipt = tag_signing.verify_remote_tag(str(publication_remote), TAG, target,
                                            policy(signer), REPOSITORY, signer['allowed'])
    assert receipt['tag'] == TAG
    assert receipt['tagObject'] == tag_object
    assert receipt['target'] == target
    assert receipt['targetType'] == 'commit'
    assert receipt['signature']['fingerprint'] == signer['fingerprint']
    run('git', '-c', 'gpg.format=ssh', '-c', f'gpg.ssh.allowedSignersFile={signer["allowed"]}',
        'verify-tag', TAG, cwd=publication_remote)


@pytest.mark.parametrize('failure', ['missing', 'lightweight', 'wrong-target', 'alias', 'tampered'])
def test_remote_verification_refuses_unqualified_publication(repo, signer, publication_remote, failure):
    target = head(repo)
    expected = 'cannot fetch'
    if failure == 'missing':
        # A perfectly signed local tag cannot substitute for an absent published ref.
        run('git', 'tag', '-s', '-m', 'local only', TAG, target, cwd=repo)
    elif failure == 'lightweight':
        run('git', 'tag', TAG, target, cwd=repo)
        run('git', 'push', str(publication_remote), f'refs/tags/{TAG}', cwd=repo)
        expected = 'not an annotated tag'
    else:
        tag_object = publish_signed(repo, signer, publication_remote)
        expected = 'exact certified candidate'
        if failure == 'wrong-target':
            (repo / 'README.md').write_text('a different candidate\n', encoding='utf-8')
            run('git', 'commit', '-qam', 'new candidate', cwd=repo)
            target = head(repo)
        elif failure == 'alias':
            alias = 'honua-2026.1.1'
            run('git', 'update-ref', f'refs/tags/{alias}', tag_object, cwd=publication_remote)
            with pytest.raises(SigningError, match='must name this publication tag'):
                tag_signing.verify_remote_tag(str(publication_remote), alias, target,
                                             policy(signer), REPOSITORY, signer['allowed'])
            return
        elif failure == 'tampered':
            payload = run('git', 'cat-file', 'tag', tag_object, cwd=repo)
            broken = subprocess.run(['git', 'hash-object', '-t', 'tag', '-w', '--stdin'],
                                    input=payload.replace('first release', 'forged release') + '\n',
                                    cwd=publication_remote, text=True, capture_output=True, check=True)
            run('git', 'update-ref', f'refs/tags/{TAG}', broken.stdout.strip(), cwd=publication_remote)
            expected = 'signature did not verify'
    with pytest.raises(SigningError, match=expected):
        tag_signing.verify_remote_tag(str(publication_remote), TAG, target,
                                     policy(signer), REPOSITORY, signer['allowed'])


@pytest.mark.parametrize('failure', [None, 'wrong-target', 'no-trust', 'no-signer'])
def test_promotion_executes_remote_tag_guard(tmp_path, repo, signer, publication_remote, failure):
    """Execute the actual promotion step on a real signed remote; assert its receipt and exit."""
    target = head(repo)
    tag_object = publish_signed(repo, signer, publication_remote)
    root = tag_signing.ROOT
    steps = yaml.safe_load((root / '.github/workflows/promote.yml').read_text())['jobs']['promote']['steps']
    preserve = next(s for s in steps if s.get('name') == 'Preserve current publication trust policy and verifier')
    guard = next(s for s in steps if 'verify-remote' in s.get('run', ''))
    temp = tmp_path / 'runner'
    temp.mkdir()
    env = {**os.environ, 'RUNNER_TEMP': str(temp), 'PUBLICATION_TAG': TAG,
           'CERTIFIED_SHA': target, 'PUBLICATION_REMOTE': str(publication_remote),
           'RELEASE_TAG_ALLOWED_SIGNERS': signer['allowed'].read_text()}
    subprocess.run(['bash', '-c', preserve['run']], cwd=root, env=env, check=True, capture_output=True)
    trust = temp / 'publication-trust/certification/release-controls/tag-signing-policy.json'
    trusted = json.loads(trust.read_text())
    if failure != 'no-signer':
        trusted['signers'] = policy(signer)['signers']
        trusted['repositories'][REPOSITORY]['signers'] = ['release-owner']
    trust.write_text(json.dumps(trusted), encoding='utf-8')
    if failure == 'wrong-target':
        env['CERTIFIED_SHA'] = 'a' * 40
    if failure == 'no-trust':
        env['RELEASE_TAG_ALLOWED_SIGNERS'] = ''
    # setup-python supplies `python` on Actions; use this interpreter on the local host.
    command = guard['run'].replace('python "$RUNNER_TEMP', f'"{sys.executable}" "$RUNNER_TEMP')
    result = subprocess.run(['bash', '-c', command], cwd=tmp_path, env=env,
                            text=True, capture_output=True)
    receipt_path = tmp_path / 'signed-tag-receipt.json'
    if failure:
        assert result.returncode != 0, result.stdout + result.stderr
        assert not receipt_path.exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads(receipt_path.read_text())
        assert receipt['target'] == target
        assert receipt['tagObject'] == tag_object
        assert receipt['signature']['fingerprint'] == signer['fingerprint']
