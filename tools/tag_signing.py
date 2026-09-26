#!/usr/bin/env python3
"""Produce and verify signed annotated publication tags against an explicit trust policy.

#236 requires signed tags on release lines. Three things that are routinely mistaken for that
are not it, and this tool refuses all three:

  * GitHub's `required_signatures` ruleset rule verifies *commit* signatures, not tag objects.
  * A ruleset `update`/`deletion` tag rule makes a tag immutable; immutability is not a signature.
  * `gh release create` writes a lightweight tag — a ref pointing straight at a commit. There is
    no tag object, so there is nothing that could carry a signature.

A qualified publication tag is an annotated tag object whose signature verifies against a
signer this repository's trust policy names by fingerprint. The trust policy holds fingerprints
only; the public key material stays in an operator-supplied allowed-signers file that is never
committed, and every principal in that file must already be named in the policy.

The policy currently names **no** signer: the release owner has not nominated a publication
signing key. Every command therefore fails closed today. That is the honest state of #236's
signing prerequisite, and it is the one input this repository cannot manufacture for itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONTROLS = ROOT / "certification/release-controls"
TRUST_POLICY = CONTROLS / "tag-signing-policy.json"
CONTROL_POLICY = CONTROLS / "policy.json"
RECEIPT_SCHEMA = "honua.signed-tag-receipt/v1"
ISSUE = "honua-io/honua-release#236"
FORMATS = ("ssh", "openpgp")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
# `git verify-tag` reports an SSH signer as `... with <TYPE> key SHA256:<base64>`; GnuPG's
# machine-readable status line carries the 40-hex OpenPGP fingerprint after VALIDSIG.
SSH_SIGNER = re.compile(r'Good "git" signature for (?P<principal>\S+) with \S+ key (?P<fingerprint>SHA256:[A-Za-z0-9+/=]+)')
PGP_SIGNER = re.compile(r"^\[GNUPG:\] VALIDSIG (?P<fingerprint>[0-9A-F]{40})", re.MULTILINE)


class SigningError(ValueError):
    """A signing or verification precondition that must fail closed."""


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_policy(path: Path = TRUST_POLICY) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    if policy.get("schema_version") != 1 or policy.get("issue") != ISSUE:
        raise SigningError("trust policy must be schema_version 1 for " + ISSUE)
    signers = policy.get("signers")
    repositories = policy.get("repositories")
    if not isinstance(signers, list) or not isinstance(repositories, dict):
        raise SigningError("trust policy needs a signers list and a repositories mapping")
    seen = set()
    for signer in signers:
        if not isinstance(signer, dict):
            raise SigningError("each signer must be a mapping")
        if signer.get("format") not in FORMATS:
            raise SigningError(f"signer format must be one of {', '.join(FORMATS)}")
        fingerprint = str(signer.get("fingerprint", ""))
        expected = (r"SHA256:[A-Za-z0-9+/=]{43,}" if signer["format"] == "ssh" else r"[0-9A-F]{40}")
        if not re.fullmatch(expected, fingerprint):
            raise SigningError(f"signer {signer.get('id')!r} needs a {signer['format']} fingerprint")
        if not signer.get("id") or signer["id"] in seen:
            raise SigningError("each signer needs a unique id")
        seen.add(signer["id"])
    for name, entry in repositories.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("tag_refs"), list):
            raise SigningError(f"{name}: trust policy entry needs tag_refs")
        unknown = [s for s in entry.get("signers", []) if s not in seen]
        if unknown:
            raise SigningError(f"{name}: unknown signer id(s) {', '.join(unknown)}")
    return policy


def check_namespaces(policy: dict[str, Any], control_policy_path: Path = CONTROL_POLICY) -> list[str]:
    """The trust policy must cover exactly the tag namespaces the control policy protects."""
    controls = json.loads(control_policy_path.read_text(encoding="utf-8"))["repositories"]
    protected = {name: row["tag_refs"] for name, row in controls.items() if row.get("tag_refs")}
    errors = []
    for name in sorted(set(protected) | set(policy["repositories"])):
        expected = protected.get(name)
        actual = policy["repositories"].get(name, {}).get("tag_refs")
        if expected is None:
            errors.append(f"{name}: trust policy names tag namespaces the control policy does not protect")
        elif actual is None:
            errors.append(f"{name}: protected publication tags have no signing trust entry")
        elif sorted(actual) != sorted(expected):
            errors.append(f"{name}: signing namespaces {actual} differ from protected {expected}")
    return errors


def authorized_signers(policy: dict[str, Any], repository: str) -> list[dict[str, Any]]:
    entry = policy["repositories"].get(repository)
    if entry is None:
        raise SigningError(f"{repository} publishes no policy-declared native tags")
    ids = entry.get("signers") or []
    signers = [signer for signer in policy["signers"] if signer["id"] in ids]
    if not signers:
        raise SigningError(f"{repository}: no publication signing key is nominated in the trust "
                           "policy; a signed tag cannot be produced or trusted")
    return signers


def check_namespace(policy: dict[str, Any], repository: str, tag: str) -> str:
    patterns = policy["repositories"][repository]["tag_refs"]
    ref = f"refs/tags/{tag}"
    for pattern in patterns:
        if fnmatch.fnmatchcase(ref, pattern):
            return pattern
    raise SigningError(f"{tag} is outside {repository}'s protected publication namespaces "
                       f"({', '.join(patterns)})")


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SigningError((result.stderr or result.stdout).strip().splitlines()[0]
                           if (result.stderr or result.stdout).strip() else "git command failed")
    return result


def key_fingerprint(public_key: str) -> str:
    result = subprocess.run(["ssh-keygen", "-lf", "-"], input=public_key,
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise SigningError("cannot read an SSH public key fingerprint")
    return result.stdout.split()[1]


def check_allowed_signers(policy: dict[str, Any], repository: str, path: Path) -> None:
    """Every key the verifier will trust must already be a policy-named signer for this repo."""
    trusted = {signer["fingerprint"] for signer in authorized_signers(policy, repository)}
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
             and not line.lstrip().startswith("#")]
    if not lines:
        raise SigningError("allowed-signers file is empty")
    for line in lines:
        parts = line.split()
        key = next((" ".join(parts[index:index + 2]) for index, part in enumerate(parts)
                    if part.startswith(("ssh-", "sk-", "ecdsa-"))), None)
        if key is None:
            raise SigningError("allowed-signers line carries no SSH public key")
        fingerprint = key_fingerprint(key)
        if fingerprint not in trusted:
            raise SigningError(f"allowed-signers names {fingerprint}, which {repository}'s trust "
                               "policy does not authorize")


def parse_verification(output: str, fmt: str) -> str:
    match = (SSH_SIGNER if fmt == "ssh" else PGP_SIGNER).search(output)
    if not match:
        raise SigningError("tag signature did not verify against the supplied trust material")
    return match.group("fingerprint")


def verify_tag(repo: Path, tag: str, policy: dict[str, Any], repository: str,
               allowed_signers: Path | None = None,
               policy_path: Path = TRUST_POLICY) -> dict[str, Any]:
    check_namespace(policy, repository, tag)
    signers = authorized_signers(policy, repository)
    formats = {signer["format"] for signer in signers}
    if len(formats) != 1:
        raise SigningError(f"{repository}: signers must agree on one signature format")
    fmt = formats.pop()
    if fmt == "ssh":
        if allowed_signers is None:
            raise SigningError("ssh tag verification needs the operator's allowed-signers file")
        check_allowed_signers(policy, repository, allowed_signers)
    kind = git(repo, "cat-file", "-t", tag).stdout.strip()
    if kind != "tag":
        raise SigningError(f"{tag} is a {kind} ref, not an annotated tag object; a lightweight "
                           "tag carries no signature")
    tag_object = git(repo, "rev-parse", tag).stdout.strip()
    target = git(repo, "rev-parse", f"{tag}^{{}}").stdout.strip()
    command = ["-c", "gpg.format=" + ("ssh" if fmt == "ssh" else "openpgp")]
    if fmt == "ssh":
        command += ["-c", f"gpg.ssh.allowedSignersFile={allowed_signers}"]
    result = git(repo, *command, "verify-tag", "--raw", tag, check=False)
    if result.returncode != 0:
        raise SigningError("tag signature did not verify: "
                           + ((result.stderr or result.stdout).strip().splitlines() or ["no output"])[-1])
    fingerprint = parse_verification(result.stderr + result.stdout, fmt)
    signer = next((s for s in signers if s["fingerprint"] == fingerprint), None)
    if signer is None:
        raise SigningError(f"tag was signed by {fingerprint}, which {repository}'s trust policy "
                           "does not authorize")
    return {
        "schema": RECEIPT_SCHEMA, "issue": ISSUE, "repository": repository, "tag": tag,
        "tagObject": tag_object, "target": target,
        "targetType": git(repo, "cat-file", "-t", target).stdout.strip(),
        "signature": {"format": fmt, "fingerprint": fingerprint, "signerId": signer["id"]},
        "policySha256": digest(policy_path),
        "verifiedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def sign_tag(repo: Path, tag: str, target: str, message: str, policy: dict[str, Any],
             repository: str, allowed_signers: Path | None = None,
             policy_path: Path = TRUST_POLICY) -> dict[str, Any]:
    """Create the annotated signed tag, then verify what was actually written."""
    check_namespace(policy, repository, tag)
    signers = authorized_signers(policy, repository)
    if not SHA1.fullmatch(target):
        raise SigningError("a publication tag must name an immutable 40-character target revision")
    if git(repo, "cat-file", "-t", target, check=False).stdout.strip() != "commit":
        raise SigningError(f"{target} is not a commit in this repository")
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}", check=False).returncode == 0:
        raise SigningError(f"{tag} already exists; publication tags are immutable and never moved")
    configured = git(repo, "config", "--get", "user.signingkey", check=False).stdout.strip()
    if not configured:
        raise SigningError("no user.signingkey is configured for the publication signer")
    fmt = signers[0]["format"]
    if fmt == "ssh":
        key = Path(configured)
        fingerprint = key_fingerprint(key.read_text(encoding="utf-8")) if key.is_file() else key_fingerprint(configured)
    else:
        fingerprint = configured.upper().removeprefix("0X")
    if fingerprint not in {signer["fingerprint"] for signer in signers}:
        raise SigningError(f"configured signing key {fingerprint} is not authorized for {repository}")
    git(repo, "tag", "-s", "-a", "-m", message, tag, target)
    return verify_tag(repo, tag, policy, repository, allowed_signers, policy_path)


def qualify_receipt(repository: str, tag_refs: list[str], receipt: Any,
                    policy_path: Path = TRUST_POLICY, *, expected: Any = None,
                    source: Any = None) -> str | None:
    """Accept a signed-tag receipt as #236 qualification, or return why it is not one.

    A receipt is an unauthenticated JSON claim about a verification someone says happened, so it
    qualifies nothing by itself: anyone able to write the file could copy the policy digest and an
    authorized fingerprint and invent two object ids. Three independent bindings are therefore
    required, and each one alone is insufficient:

      * the committed trust policy bytes that authorized the signer;
      * the exact release identity being audited — a receipt for an old or throwaway signed tag
        must not clear the control for the tag the audited candidate actually publishes;
      * a fresh cryptographic re-verification of the tag object in a real repository, which is
        what proves a signature exists at all.
    """
    if not isinstance(receipt, dict) or receipt.get("schema") != RECEIPT_SCHEMA:
        return f"expected a {RECEIPT_SCHEMA} receipt"
    if receipt.get("repository") != repository:
        return f"receipt describes {receipt.get('repository')!r}, not {repository}"
    if receipt.get("policySha256") != digest(policy_path):
        return "receipt is bound to different trust policy bytes than the committed policy"
    for field in ("tagObject", "target"):
        if not SHA1.fullmatch(str(receipt.get(field, ""))):
            return f"receipt {field} must be an immutable git object id"
    if receipt["tagObject"] == receipt["target"]:
        return "receipt names a lightweight tag; an annotated tag object is required"
    ref = f"refs/tags/{receipt.get('tag')}"
    if not any(fnmatch.fnmatchcase(ref, pattern) for pattern in tag_refs):
        return f"{receipt.get('tag')!r} is outside the protected publication namespaces"
    signature = receipt.get("signature")
    if not isinstance(signature, dict) or signature.get("format") not in FORMATS:
        return "receipt carries no signature format"
    try:
        policy = load_policy(policy_path)
        signers = authorized_signers(policy, repository)
    except (OSError, SigningError, json.JSONDecodeError) as exc:
        return str(exc)
    if signature.get("fingerprint") not in {signer["fingerprint"] for signer in signers}:
        return "receipt fingerprint is not an authorized publication signer"
    # A namespace pattern is not a release identity: `refs/tags/v*` matches any old signed tag.
    if not isinstance(expected, dict) or not expected.get("tag") or not SHA1.fullmatch(
            str(expected.get("target", ""))):
        return ("no audited release identity (publication tag and candidate revision) is supplied "
                "to bind the receipt to")
    if receipt["tag"] != expected["tag"]:
        return f"receipt is for {receipt['tag']!r}, not the audited release tag {expected['tag']!r}"
    if receipt["target"] != expected["target"]:
        return "receipt target is not the audited candidate revision"
    if not isinstance(source, dict) or not source.get("gitDir"):
        return ("receipt is unauthenticated; qualification requires re-verifying the tag object in "
                "the repository, so a gitDir must be supplied")
    allowed = source.get("allowedSigners")
    try:
        fresh = verify_tag(Path(source["gitDir"]), receipt["tag"], policy, repository,
                           Path(allowed) if allowed else None, policy_path)
    except (OSError, SigningError) as exc:
        return f"re-verification of the signed tag failed: {exc}"
    for field in ("tagObject", "target", "targetType", "signature"):
        if fresh[field] != receipt[field]:
            return f"receipt {field} disagrees with the tag object that actually verified"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", type=Path, default=TRUST_POLICY)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("check-policy", help="validate the trust policy against the control policy")
    for name, help_text in (("sign", "create a signed annotated publication tag"),
                            ("verify", "verify an existing publication tag")):
        sub = subs.add_parser(name, help=help_text)
        sub.add_argument("repository")
        sub.add_argument("tag")
        sub.add_argument("--git-dir", type=Path, default=Path.cwd())
        sub.add_argument("--allowed-signers", type=Path)
        sub.add_argument("--output", type=Path)
        if name == "sign":
            sub.add_argument("--target", required=True)
            sub.add_argument("--message", required=True)
    args = parser.parse_args(argv)
    try:
        policy = load_policy(args.policy)
        if args.command == "check-policy":
            errors = check_namespaces(policy)
            if errors:
                raise SigningError("; ".join(errors))
            print(f"PASS: trust policy covers {len(policy['repositories'])} protected tag "
                  f"namespace set(s); {len(policy['signers'])} signer(s) nominated")
            return 0
        if args.command == "sign":
            receipt = sign_tag(args.git_dir, args.tag, args.target, args.message, policy,
                               args.repository, args.allowed_signers, args.policy)
        else:
            receipt = verify_tag(args.git_dir, args.tag, policy, args.repository,
                                 args.allowed_signers, args.policy)
        if args.output:
            args.output.write_text(json.dumps(receipt, indent=2) + "\n",
                                   encoding="utf-8", newline="\n")
        print(json.dumps(receipt, indent=2))
        return 0
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
