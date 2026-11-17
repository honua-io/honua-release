"""The published-SDK rehearsal binds the pinned commit even when npm omits gitHead."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from resource_rehearsal import published_source_commit  # noqa: E402

COMMIT = "9d078e17abf7c90cecd000df6fcda4f3d5d9548f"
ATTESTATIONS = "https://registry.npmjs.org/-/npm/v1/attestations/@honua%2Fsdk-js@0.4.0"


def _attestation(uri):
    statement = {"predicate": {"buildDefinition": {"resolvedDependencies": [
        {"uri": uri, "digest": {"gitCommit": COMMIT}}]}}}
    payload = base64.b64encode(json.dumps(statement).encode()).decode()
    return json.dumps({"attestations": [{"bundle": {"dsseEnvelope": {"payload": payload}}}]}).encode()


def test_trusted_publish_without_git_head_uses_the_attested_commit():
    def fetch(url, accept=None, token=None):
        assert url == ATTESTATIONS
        return _attestation("git+https://github.com/honua-io/honua-sdk-js@refs/heads/trunk")

    assert published_source_commit({"version": "0.4.0"}, fetch) == COMMIT


def test_git_head_wins_without_reading_attestations():
    def fetch(url, accept=None, token=None):
        raise AssertionError(url)

    assert published_source_commit({"version": "0.4.0", "gitHead": "a" * 40}, fetch) == "a" * 40


def test_an_attestation_from_another_repository_binds_nothing():
    for uri in ("git+https://github.com/other/repo@refs/heads/trunk",
                "git+https://github.com/honua-io/honua-sdk-js-legacy@refs/heads/trunk",
                "git+https://github.com/evil/x?github.com/honua-io/honua-sdk-js@refs/heads/trunk"):
        def fetch(url, accept=None, token=None, uri=uri):
            return _attestation(uri)

        assert published_source_commit({"version": "0.4.0"}, fetch) == "", uri
