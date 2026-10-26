"""Controls for the live portal group-sharing producer's classification and binding rules."""
from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("portal_sharing_produce", HERE / "produce.py")
produce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(produce)

ITEM = "svc-publish-portal691"
REFUSAL = {"error": {"code": 403, "message": "Forbidden"}}


def test_server_pin_binds_tag_and_index_digest():
    digest, amd64 = "sha256:" + "a" * 64, "sha256:" + "e" * 64
    text = (f'components:\n  honua-server:\n    sha: "{"b" * 40}"\n    image: "ghcr.io/x/server:nightly-b"\n'
            f'    digest: "{digest}"\n    platformDigests:\n      amd64: "{amd64}"\n')
    pin = produce.server_pin(text)
    assert pin == {"sourceSha": "b" * 40, "image": f"ghcr.io/x/server:nightly-b@{digest}", "digest": digest,
                   "amd64": amd64}
    for broken in (text.replace(digest, "sha256:short"), text.replace(amd64, "sha256:short"), text.replace('"' + "b" * 40 + '"', '"abc"'),
                   text.replace("server:nightly-b", f"server@{digest}"), "components: {}\n"):
        with pytest.raises(produce.ProducerError):
            produce.server_pin(broken)


def test_the_manifest_pin_is_loadable():
    assert produce.server_pin((produce.ROOT / "platform-manifest.yaml").read_text())["image"].count("@sha256:") == 1


def test_bearer_is_an_hs256_token_for_the_per_run_issuer():
    token = produce.mint_bearer("k" * 64, "portal-member", ["org_user"], "default")
    header, body, signature = token.split(".")
    expected = base64.urlsafe_b64encode(hmac.new(b"k" * 64, f"{header}.{body}".encode(), hashlib.sha256).digest()).rstrip(b"=")
    assert signature.encode() == expected
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    assert (claims["iss"], claims["aud"], claims["sub"], claims["roles"], claims["tenant_id"]) == (
        produce.ISSUER, produce.AUDIENCE, "portal-member", ["org_user"], "default")


def test_reads_are_visible_only_on_the_real_effect():
    assert produce.item_visible({"id": ITEM}, ITEM) is True
    assert produce.item_visible({"error": {"code": 404}}, ITEM) is False
    assert produce.search_visible({"results": [{"id": ITEM}]}, ITEM) is True
    assert produce.search_visible({"results": [{"id": "other"}]}, ITEM) is False
    assert produce.group_content_visible({"items": [{"id": ITEM}]}, ITEM) is True
    assert produce.group_content_visible({"items": []}, ITEM) is False
    assert produce.group_content_visible(REFUSAL, ITEM) is False
    assert produce.feature_visible({"count": 3}, 3) is True
    assert produce.feature_visible({"error": {"code": 499}}, 3) is False


@pytest.mark.parametrize("call", [
    lambda: produce.item_visible({"id": "other"}, ITEM),          # another item is not this item
    lambda: produce.item_visible({"error": {"code": 500}}, ITEM),  # a server fault is not a refusal
    lambda: produce.search_visible({"error": {"code": 403}}, ITEM),
    lambda: produce.group_content_visible({"error": {"code": 500}}, ITEM),
    lambda: produce.feature_visible({"count": 2}, 3),               # a partial read is not the stored rows
    lambda: produce.feature_visible({"count": 0}, 3),
    lambda: produce.feature_visible({"error": {"code": 400}}, 3),
    lambda: produce.feature_visible(None, 3),
])
def test_ambiguous_reads_fail_the_run(call):
    with pytest.raises(produce.ProducerError):
        call()


def test_redaction_removes_every_secret_including_nested_occurrences():
    secrets_ = ["tok-123", "tok-123456", "key-xyz"]
    redacted = produce.redact({"a": "tok-123456", "b": ["x key-xyz y"], "c": {"token": "tok-123"}}, secrets_)
    assert "tok-123" not in json.dumps(redacted) and "key-xyz" not in json.dumps(redacted)


def test_the_judge_binds_the_producer_document_to_its_candidate():
    judge = produce.judge_module()
    view = lambda value: {name: value for name in judge.SURFACES}  # noqa: E731
    shared = {"member": view(True), "nonmember": view(False)}
    hidden = {"member": view(False), "nonmember": view(False)}
    pin = {"sourceSha": "c" * 40, "image": "ghcr.io/x/server:t@sha256:" + "d" * 64}
    document = {
        "schema": produce.SCHEMA, "candidateSourceSha": pin["sourceSha"], "candidateImage": pin["image"],
        "identities": {"owner": "o", "member": "m", "nonmember": "n", "group": "g", "item": ITEM},
        "runtime": {"beforeRestart": "s1", "afterRestart": "s2", "replica": "r", "store": "postgresql:1"},
        "notSharedWith": ["everyone", "org"], "publicCallerVisibilityUnchanged": True,
        "policyRefusals": {name: "refused" for name in judge.POLICY_REFUSALS},
        "phases": {"shared": shared, "sharedAfterRestart": shared, "sharedOnReplica": shared,
                   "unshared": hidden, "unsharedAfterRestart": hidden},
    }
    assert produce.SCHEMA == judge.SCHEMA
    assert set(produce.PRINCIPALS) >= {"owner", "member", "nonmember"}
    assert judge.qualify(document, candidate=pin)["status"] == "pass"
    assert judge.qualify(document)["status"] == "nonpass"
