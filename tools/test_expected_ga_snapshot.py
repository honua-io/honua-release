"""tools/expected_ga_snapshot.py: the observation is generated from a boot of the candidate server
and bound to its sha, index digest and booted child, or nothing is written."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import expected_ga_snapshot as snap  # noqa: E402

ego = snap.ego
SHA = "a" * 40
INDEX = "sha256:" + "1" * 64
AMD64 = "sha256:" + "2" * 64
ARM64 = "sha256:" + "3" * 64
SERVER = {"sha": SHA, "image": f"ghcr.io/honua-io/honua-server@{INDEX}", "digest": INDEX,
          "platformDigests": {"amd64": AMD64, "arm64": ARM64}}


def manifest(revision=SHA, source="commit-sha", caps=None):
    return {"schemaVersion": "honua.capability_manifest.v1",
            "server": {"deploymentRevision": revision, "deploymentRevisionSource": source},
            "capabilities": caps if caps is not None else [
                {"id": "a.one", "lifecycle": "ga", "supported": True, "available": False},
                {"id": "b.two", "lifecycle": "preview", "supported": True, "available": False}]}


def run(server=SERVER, *, children=None, anonymous=None, authenticated=None, label=SHA, booted=None):
    booted = booted if booted is not None else []
    auth = authenticated if authenticated is not None else manifest(caps=[
        {"id": "a.one", "lifecycle": "ga", "supported": True, "available": True},
        {"id": "b.two", "lifecycle": "preview", "supported": True, "available": False}])
    return snap.generate(
        server, base_url="http://candidate", api_key="k",
        inspector=lambda coordinate, digest: children or {"amd64": AMD64, "arm64": ARM64},
        boot=booted.append, fetch=lambda url, key=None: auth if key else (anonymous or manifest()),
        host_arch=lambda: "amd64", image_label=lambda image: label, now=lambda: "2026-10-26T00:00:00+00:00")


def test_the_observation_boots_the_candidate_index_and_binds_sha_index_and_child():
    booted = []
    observation = run(booted=booted)
    assert booted == [f"ghcr.io/honua-io/honua-server@{INDEX}"]
    assert observation["boundCandidate"] == {"serverSha": SHA, "image": booted[0], "imageIndexDigest": INDEX,
                                             "bootedPlatform": "amd64", "bootedChildDigest": AMD64}
    assert observation["capabilities"][0] == {"id": "a.one", "lifecycle": "ga", "supported": True,
                                              "available": False, "availableAuthenticated": True}
    assert ego.binding_errors(observation, SERVER) == []
    # A tag-pinned manifest image still boots by its immutable index digest.
    tagged = {**SERVER, "image": "ghcr.io/honua-io/honua-server:nightly-aaaaaaa"}
    booted = []
    run(tagged, booted=booted)
    assert booted == [f"ghcr.io/honua-io/honua-server@{INDEX}"]


@pytest.mark.parametrize("kwargs, problem", [
    ({"anonymous": manifest(revision="b" * 40)}, "not the candidate"),
    ({"anonymous": manifest(source="manifest")}, "not the candidate"),
    ({"label": "b" * 40}, "labels revision"),
    ({"children": {"amd64": "sha256:" + "9" * 64}}, "is not the candidate platformDigests.amd64"),
    ({"authenticated": manifest(caps=[{"id": "a.one", "lifecycle": "ga", "supported": True}])},
     "different ids"),
    ({"authenticated": manifest(caps=[{"id": "a.one", "lifecycle": "ga", "supported": False},
                                      {"id": "b.two", "lifecycle": "preview", "supported": True}])},
     "supported"),
])
def test_a_boot_that_is_not_the_candidate_server_refuses(kwargs, problem):
    with pytest.raises(ValueError, match=problem):
        run(**kwargs)


def test_another_candidate_refuses_a_generated_observation():
    observation = run()
    for field, value in (("sha", "c" * 40), ("digest", "sha256:" + "4" * 64)):
        assert ego.binding_errors(observation, {**SERVER, field: value})
    assert ego.binding_errors(observation, {**SERVER, "platformDigests": {"amd64": ARM64}})
    assert ego.binding_errors(observation, {"sha": SHA})   # unknown digest cannot bind
    assert ego.binding_errors(None, SERVER)


def test_policy_ids_must_be_supported_by_the_bound_boot():
    observation = run()
    assert ego.policy_errors({"expectedGa": ["a.one", "z.gone"], "excluded": [{"id": "z.gone"}]}, observation) == []
    assert "z.gone" in ego.policy_errors({"expectedGa": ["a.one", "z.gone"]}, observation)[0]
    assert ego.policy_errors(None, observation)


def test_cli_writes_nothing_when_the_boot_refuses(monkeypatch, tmp_path):
    path = tmp_path / "manifest.yaml"
    path.write_text(json.dumps({"components": {"honua-server": SERVER}}), encoding="utf-8")
    out = tmp_path / "observation.json"
    generated = run()
    downs = []
    monkeypatch.setattr(snap, "generate", lambda server, **_: (_ for _ in ()).throw(ValueError("not the candidate")))
    monkeypatch.setattr(snap.subprocess, "run", lambda cmd, **_: downs.append(cmd))
    assert snap.main(["--manifest", str(path), "--out", str(out)]) == 1
    assert not out.exists()
    assert downs and downs[-1][-1] == "down"   # the booted stack is always torn down

    monkeypatch.setattr(snap, "generate", lambda server, **_: generated)
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"expectedGa": ["a.one"]}), encoding="utf-8")
    assert snap.main(["--manifest", str(path), "--out", str(out), "--policy", str(policy)]) == 0
    assert ego.binding_errors(json.loads(out.read_text()), SERVER) == []
    policy.write_text(json.dumps({"expectedGa": ["z.missing"]}), encoding="utf-8")
    out.unlink()
    assert snap.main(["--manifest", str(path), "--out", str(out), "--policy", str(policy)]) == 1
    assert not out.exists()


def test_committed_observation_is_the_committed_server_and_satisfies_the_policy():
    import yaml
    server = yaml.safe_load((snap.ROOT / "platform-manifest.yaml").read_text())["components"]["honua-server"]
    observation = ego.load_json(ego.OBSERVATION_PATH)
    assert ego.binding_errors(observation, server) == []
    assert ego.policy_errors(ego.load_json(ego.POLICY_PATH), observation) == []
    assert copy.deepcopy(observation)["totalCapabilities"] == len(observation["capabilities"])
