import hashlib
import importlib.util
import json
import shutil

import jsonschema
import pytest
import yaml
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("convergence_rebind", ROOT / "tools/convergence_rebind.py")
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class StubGitHub:
    def __init__(self, root):
        self.root = root

    def head(self, repository):
        return {"honua-io/honua-esri-compat": "1" * 40, "cloudnativegeo/cloud-optimized-geospatial-formats-guide": "2" * 40}[repository]

    def content(self, repository, path, revision):
        local = next(local for source, mappings in MODULE.VENDORED.items() for upstream, local in mappings if upstream == path and repository.endswith(MODULE.load_json(self.root, MODULE.REVISIONS)["sources"][source]["repository"].split("/")[-1]))
        return (self.root / local).read_bytes()


def fixture(tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(ROOT, root, ignore=shutil.ignore_patterns(".git", "__pycache__", "rebind-plan.json"))
    return root


def align_components_to_published(root: Path) -> None:
    """Point component SHAs at the published sourceShas without touching the ledger."""
    path = root / MODULE.MANIFEST
    text = path.read_text(encoding="utf-8")
    manifest = yaml.safe_load(text)
    for _source, component, artifact in MODULE.SDK_PRODUCERS:
        current = manifest["components"][component]["sha"]
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        if current != published:
            text = MODULE.replace_scalar(text, "sha", current, published)
    path.write_text(text, encoding="utf-8")
    assert yaml.safe_load(text)["protocolCertification"]["ledger"]["status"] == "pending"


def diverge_sdk_js(root: Path) -> None:
    """Move components.honua-sdk-js off its published sourceSha, as a trunk re-pin would.

    The committed manifest now pins every SDK at its published source (R25), so a refusal test
    has to create the divergence it refuses rather than rely on the live pins."""
    path = root / MODULE.MANIFEST
    text = path.read_text(encoding="utf-8")
    current = yaml.safe_load(text)["components"]["honua-sdk-js"]["sha"]
    path.write_text(MODULE.replace_scalar(text, "sha", current, "e" * 40), encoding="utf-8")


def test_plan_refuses_sdk_component_pins_that_are_not_published(tmp_path):
    root = fixture(tmp_path)
    diverge_sdk_js(root)
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.prepare(root, StubGitHub(root), "keep")
    message = str(exc.value)
    assert "protocolCertification.ledger stays pending" in message
    assert "sdk-js" in message and "e" * 40 in message
    manifest = yaml.safe_load(before)
    for source, component, artifact in MODULE.SDK_PRODUCERS:
        component_sha = manifest['components'][component]['sha']
        published_sha = manifest['clientArtifacts'][artifact]['sourceSha']
        if component_sha != published_sha:
            assert source in message
            assert component_sha in message
            assert published_sha in message
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert yaml.safe_load(before)["protocolCertification"]["ledger"] == {
        "status": "pending",
        "repository": "honua-io/honua-evidence",
        "commit": "pending",
        "requirementsSourceRevision": "pending",
        "path": "data/protocol-certification.v1.json",
        "sha256": "pending",
    }


def test_cli_refuses_divergent_sdk_pins_without_writing_a_plan(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    diverge_sdk_js(root)
    monkeypatch.setattr(MODULE, "ROOT", root)
    plan_path = tmp_path / "rebind-plan.json"
    assert MODULE.main(["--plan-output", str(plan_path)]) == 1
    assert not plan_path.exists()


def test_plan_targets_published_sdk_pins_when_components_match(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    pins = {row["source"]: (row["target"], row["rule"]) for row in plan["sources"]}
    manifest = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))
    for source, component, artifact in MODULE.SDK_PRODUCERS:
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        assert pins[source] == (published, "manifest/frozen")
        assert manifest["components"][component]["sha"] == published
    assert pins["server-certification"] == ("87966c3f7b6c840ffc4d4da0b451714ab717b18a", "manifest/frozen")
    assert plan["receipt_schema_min"] == {"current": "v2", "proposed": "v2"}
    assert plan["bindings"]["PROTOCOL_CERTIFICATION_MATRIX_COMMIT"]["current"] == "pending"
    assert manifest["protocolCertification"]["ledger"]["status"] == "pending"


def test_apply_changes_only_planned_repository_files(tmp_path, monkeypatch):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, payloads, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *a, **kw: type("R", (), {"stdout": "", "stderr": "", "returncode": 0})())
    # Assert the complete intended payload rather than mutating the real worktree.
    assert set(payloads) == {str(MODULE.REVISIONS), str(MODULE.CATALOG), "certification/generate-protocol-requirements.py", *(p for mappings in MODULE.VENDORED.values() for _, p in mappings)}
    assert tuple(MODULE.CALLERS) == (Path(".github/workflows/release-train.yml"),)


@pytest.mark.parametrize("ledger_status", ["pending", "bound"])
def test_finalize_updates_manifest_pins_and_receipt_together(tmp_path, ledger_status):
    root = fixture(tmp_path)
    align_components_to_published(root)
    manifest_path = root / MODULE.MANIFEST
    manifest = manifest_path.read_text()
    current = yaml.safe_load(manifest)["protocolCertification"]["ledger"]["status"]
    manifest_path.write_text(MODULE.replace_scalar(manifest, "status", current, ledger_status))
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    requirements = "a" * 40
    evidence = "b" * 40
    digest = "sha256:" + "c" * 64

    MODULE.finalize(root, plan, requirements, evidence, digest)

    manifest = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    assert yaml.safe_load(manifest)["protocolCertification"]["ledger"]["status"] == "bound"
    assert MODULE.scalar(manifest, "commit") == evidence
    assert MODULE.scalar(manifest, "requirementsSourceRevision") == requirements
    assert MODULE.scalar(manifest, "sha256") == digest
    for caller in MODULE.CALLERS:
        assert f"gate-protocol-certification.yml@{requirements}" in (root / caller).read_text()
    receipt = (root / "rebind-receipt.md").read_text()
    for value in (requirements, evidence, digest, "Closes #191", "Refs #180 #181 #182 #187 #188"):
        assert value in receipt
    assert "merged with a merge commit (not squash- or rebase-merged)" in receipt


def test_finalize_does_not_bind_when_published_pins_differ(tmp_path):
    root = fixture(tmp_path)
    diverge_sdk_js(root)
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    plan = {"sources": [], "bindings": {}, "receipt_schema_min": {"current": "v2", "proposed": "v2"}}
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "sha256:" + "c" * 64)
    assert "stays pending" in str(exc.value)
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert not (root / "rebind-receipt.md").exists()


def test_finalize_rejects_plan_target_that_is_not_the_published_pin(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    manifest = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))
    before = (root / MODULE.MANIFEST).read_text(encoding="utf-8")
    plan = {"sources": [], "bindings": {}, "receipt_schema_min": {"current": "v2", "proposed": "v2"}}
    for source, _component, artifact in MODULE.SDK_PRODUCERS:
        published = manifest["clientArtifacts"][artifact]["sourceSha"]
        target = "f" * 40 if source == "sdk-js" else published
        plan["sources"].append({"source": source, "current": published, "target": target, "rule": "manifest/frozen"})
    with pytest.raises(MODULE.Finding) as exc:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "sha256:" + "c" * 64)
    assert "not published clientArtifacts.honua-sdk-js.sourceSha" in str(exc.value)
    assert (root / MODULE.MANIFEST).read_text(encoding="utf-8") == before
    assert yaml.safe_load(before)["protocolCertification"]["ledger"]["status"] == "pending"


def test_finalize_rejects_noncanonical_fingerprints(tmp_path):
    root = fixture(tmp_path)
    align_components_to_published(root)
    plan, _, _ = MODULE.prepare(root, StubGitHub(root), "keep")
    try:
        MODULE.finalize(root, plan, "a" * 40, "b" * 40, "not-a-digest")
    except MODULE.Finding as exc:
        assert "sha256" in str(exc)
    else:
        raise AssertionError("noncanonical digest was accepted")


# ── nightly: produce and bind the ledger for the resolved candidate (honua-release#386) ───────

NIGHT = json.loads((ROOT / "tools/fixtures/protocol-ledger/night.json").read_text(encoding="utf-8"))


def night():
    return json.loads(json.dumps(NIGHT))


def resolved_manifest() -> dict:
    """The trunk manifest as the nightly resolver leaves it: SDKs at their published sources, the
    recorded server candidate selected, and the ledger pending until tonight's ledger is bound."""
    manifest = yaml.safe_load((ROOT / MODULE.MANIFEST).read_text(encoding="utf-8"))
    for _source, component, artifact in MODULE.SDK_PRODUCERS:
        manifest["components"][component]["sha"] = manifest["clientArtifacts"][artifact]["sourceSha"]
    candidate = NIGHT["candidate"]
    server = manifest["components"]["honua-server"]
    server.update(sha=candidate["server_sha"], digest=candidate["image_digest"], image=candidate["server_image"])
    manifest["candidate"] = {"ref": candidate["server_sha"], "refSource": "trunk"}
    certification = manifest["protocolCertification"]
    certification.update(candidateCutAt=candidate["cut_at"], serverCertificationProducerSha=candidate["server_sha"])
    certification["ledger"].update(status="pending", commit="pending", requirementsSourceRevision="pending", sha256="pending")
    return manifest


def candidate_root(tmp_path, manifest=None) -> Path:
    root = tmp_path / "candidate"
    root.mkdir(parents=True, exist_ok=True)
    (root / MODULE.MANIFEST).write_text(yaml.safe_dump(manifest or resolved_manifest(), sort_keys=False), encoding="utf-8")
    (root / "certification").mkdir(exist_ok=True)
    (root / MODULE.CATALOG).write_text(json.dumps(NIGHT["catalog"], indent=2) + "\n", encoding="utf-8")
    return root


class RecordedEvidence:
    """honua-evidence as GitHub answered it: ledger commits on trunk and their raw bytes."""

    def __init__(self, ledgers: dict[str, dict]):
        self.ledgers = {commit: (json.dumps(ledger, indent=2) + "\n").encode() for commit, ledger in ledgers.items()}
        self.queries = []

    def commits(self, repository, branch, path, since):
        self.queries.append((repository, branch, path, since))
        return list(self.ledgers)

    def raw(self, repository, path, revision):
        return self.ledgers[revision]


def refusal(ledger=None, runs=None, catalog=None, manifest=None, dispatch_id=None) -> str:
    recorded = night()
    with pytest.raises(MODULE.Finding) as refused:
        MODULE.verify_nightly_ledger(ledger or recorded["ledger"], catalog or recorded["catalog"],
                                     manifest or resolved_manifest(), recorded["requirements_revision"],
                                     recorded["runs"] if runs is None else runs, dispatch_id or recorded["dispatch_id"])
    assert "protocolCertification.ledger stays pending" in str(refused.value)
    return str(refused.value)


def rehash(cell: dict) -> dict:
    """Re-address a cell whose receipt was edited, the way honua-evidence publishes it: the digest of
    the canonical receipt bytes, the content-addressed URI and every facet bound to that digest. The
    cell stays schema-valid and self-consistent, so only the edited identity can refuse it."""
    cell["evidence_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        cell["evidence_receipt"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    cell["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + cell["evidence_digest"][7:]
    for facet in cell["facet_results"].values():
        facet["evidence_digest"] = cell["evidence_digest"]
    return cell


def schema_errors(ledger: dict) -> list[str]:
    schema = json.loads((ROOT / "certification/protocol-certification.v1.schema.json").read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker())
    return [f"{list(error.path)}: {error.message}" for error in validator.iter_errors(ledger)]


def test_nightly_fixture_ledger_is_schema_valid():
    # The fixture is what aggregate.yml commits: every pass and fail is a content-addressed
    # evidence.honua.io receipt, never a GitHub run URL, so run identity must live in the receipt.
    recorded = night()
    assert schema_errors(recorded["ledger"]) == []
    observed = [cell for cell in recorded["ledger"]["cells"] if cell["result"] in ("pass", "fail")]
    assert observed and all(cell["evidence_uri"].startswith("https://evidence.honua.io/data/sha256/") for cell in observed)
    assert all(row["dispatch_id"] == recorded["dispatch_id"] for row in recorded["runs"])


def test_nightly_bound_ledger_for_the_resolved_server_passes_freeze(tmp_path):
    recorded = night()
    root = candidate_root(tmp_path)
    evidence = RecordedEvidence({recorded["evidence_commit"]: recorded["ledger"]})

    result = MODULE.nightly_bind(root, evidence, recorded["requirements_revision"], recorded["runs"], "2026-10-03T12:50:00Z",
                                 recorded["dispatch_id"])

    assert result["results"] == {"pass": 2, "fail": 1, "skip": 1, "not-addressable": 1}
    bound = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))["protocolCertification"]["ledger"]
    assert bound == {
        "status": "bound", "repository": "honua-io/honua-evidence", "commit": recorded["evidence_commit"],
        "requirementsSourceRevision": recorded["requirements_revision"], "path": "data/protocol-certification.v1.json",
        "sha256": "sha256:" + hashlib.sha256(evidence.ledgers[recorded["evidence_commit"]]).hexdigest(),
    }
    assert evidence.queries == [("honua-io/honua-evidence", "trunk", "data/protocol-certification.v1.json", "2026-10-03T12:50:00Z")]

    # Freeze's exact-candidate check: tonight's binding is the only change it needs, and it adds no
    # other finding (the staged catalog pins the SDKs the manifest ships).
    import validate_platform as validate
    matrix = yaml.safe_load((ROOT / "compatibility-matrix.yaml").read_text(encoding="utf-8"))
    catalog = json.loads((ROOT / MODULE.CATALOG).read_text(encoding="utf-8"))
    pending = resolved_manifest()
    for source, component, _artifact in MODULE.SDK_PRODUCERS:
        catalog["source_revisions"][source]["commit"] = pending["components"][component]["sha"]
    bound_manifest = yaml.safe_load((root / MODULE.MANIFEST).read_text(encoding="utf-8"))
    before = set(validate.validate(pending, matrix, None, exact_candidate=True, requirements=catalog).errors)
    after = set(validate.validate(bound_manifest, matrix, None, exact_candidate=True, requirements=catalog).errors)
    unbound = "exact-candidate: protocol certification ledger must be bound before certification"
    assert unbound in before
    assert after == before - {unbound}
    assert not [error for error in after if "protocol certification ledger" in error]


def test_nightly_refuses_a_stale_ledger(tmp_path):
    recorded = night()
    stale = night()["ledger"]
    # The August 24 snapshot: the only ledger that existed before the nightly produced its own.
    stale["candidate"] = {"source_sha": "e3ab87cebb7bf2d32c4e8cdb145f8d626b864d8e",
                          "image_digest": "sha256:d7a45c871bf318b4882ec8e1c32004803e6d0210246be30120751f05dee1a14d",
                          "cut_at": "2026-08-21T15:13:36Z"}
    stale["requirements_source_revision"] = "47ec70604bb0db43b03f32dac1b114f1154bc2ca"
    assert "candidate" in refusal(ledger=stale)

    # Nothing in the evidence history is tonight's ledger: no commit is bound.
    root = candidate_root(tmp_path)
    before = (root / MODULE.MANIFEST).read_bytes()
    with pytest.raises(MODULE.Finding) as missing:
        MODULE.nightly_bind(root, RecordedEvidence({"c595f9d6" + "0" * 32: stale}),
                            recorded["requirements_revision"], recorded["runs"], "2026-10-03T12:50:00Z",
                            recorded["dispatch_id"])
    assert "found 0" in str(missing.value)
    assert (root / MODULE.MANIFEST).read_bytes() == before

    # A cell copied from an earlier night carries that night's observation time.
    copied = night()["ledger"]
    copied["cells"][0]["started_at"] = "2026-10-02T11:40:00Z"
    assert "predates the candidate cut" in refusal(ledger=copied)


def test_nightly_refuses_a_same_pin_run_this_nightly_did_not_dispatch(tmp_path):
    """R20/R21: a hand-dispatched producer run after the cut, at the same pins and about the same
    candidate, is still not tonight's evidence. Only the run this nightly dispatched binds."""
    recorded = night()
    sdk = recorded["runs"][1]

    # Same producer, same pin, same candidate, after the cut: only the run id differs.
    foreign = night()["ledger"]
    foreign["cells"][2]["evidence_receipt"]["identity"]["producer_run"]["run_id"] = 36000000
    rehash(foreign["cells"][2])
    assert schema_errors(foreign) == []
    assert f"is not honua-sdk-python run {sdk['run_id']} attempt 1 dispatched by {recorded['dispatch_id']}" in refusal(ledger=foreign)

    # The same run id from another producer repository or workflow is not that run either.
    for field, value in (("repository", "honua-io/honua-sdk-js"), ("workflow", "manual-conformance.yml")):
        moved = night()["ledger"]
        moved["cells"][2]["evidence_receipt"]["identity"]["producer_run"][field] = value
        rehash(moved["cells"][2])
        assert schema_errors(moved) == []
        assert "is not honua-sdk-python run" in refusal(ledger=moved)

    # A failed first attempt does not stand in for the attempt that concluded success.
    earlier = night()["ledger"]
    earlier["cells"][2]["evidence_receipt"]["identity"]["producer_run"]["run_attempt"] = 2
    rehash(earlier["cells"][2])
    assert "is not honua-sdk-python run" in refusal(ledger=earlier)

    # Another night's correlation id, carried by a run with tonight's id, is not this dispatch.
    other_night = night()["ledger"]
    other_night["cells"][0]["evidence_receipt"]["identity"]["producer_run"]["dispatch_id"] = "nightly-certification-37100000-1"
    rehash(other_night["cells"][0])
    assert "dispatched by nightly-certification-37199990-1" in refusal(ledger=other_night)

    # A receipt with no run identity cannot be attributed to any run: refused, never assumed.
    anonymous = night()["ledger"]
    del anonymous["cells"][1]["evidence_receipt"]["identity"]["producer_run"]
    rehash(anonymous["cells"][1])
    assert schema_errors(anonymous) == []
    assert "harness tiles.tile: fail carries no producer run identity" in refusal(ledger=anonymous)
    unreceipted = night()["ledger"]
    unreceipted["cells"][1].update(evidence_receipt=None, evidence_digest=None, evidence_uri=None, facet_results=None)
    assert "harness tiles.tile: fail carries no producer run identity" in refusal(ledger=unreceipted)

    # The run identity must be in the receipt the evidence URI addresses, not beside it.
    detached = night()["ledger"]
    detached["cells"][2]["evidence_receipt"]["identity"]["producer_run"]["run_id"] = 36000000
    assert "evidence_digest is not the digest of its receipt" in refusal(ledger=detached)
    readdressed = night()["ledger"]
    readdressed["cells"][2]["evidence_uri"] = "https://evidence.honua.io/data/sha256/" + "9" * 64
    assert "evidence_uri does not address its receipt" in refusal(ledger=readdressed)

    # The runs file itself must come from this nightly's dispatch step.
    replayed = night()["runs"]
    replayed[1]["dispatch_id"] = "nightly-certification-37100000-1"
    assert "producer honua-sdk-python run 37200002 was not dispatched by nightly-certification-37199990-1" in refusal(runs=replayed)
    assert "was not dispatched by nightly-certification-37100000-1" in refusal(dispatch_id="nightly-certification-37100000-1")
    assert "is not a nightly correlation id" in refusal(dispatch_id=recorded["dispatch_id"] + "\n")

    # Matching identity binds (the bound-ledger test above), and an unrelated nightly id refuses
    # bind before anything is written.
    root = candidate_root(tmp_path)
    before = (root / MODULE.MANIFEST).read_bytes()
    with pytest.raises(MODULE.Finding):
        MODULE.nightly_bind(root, RecordedEvidence({recorded["evidence_commit"]: foreign}), recorded["requirements_revision"],
                            recorded["runs"], "2026-10-03T12:50:00Z", recorded["dispatch_id"])
    assert (root / MODULE.MANIFEST).read_bytes() == before


def test_nightly_refuses_a_missing_producer():
    recorded = night()
    assert "producer honua-sdk-python has no successful run tonight" in refusal(runs=recorded["runs"][:1])
    skipped = night()["runs"]
    skipped[1]["conclusion"] = "skipped"
    assert "producer honua-sdk-python has no successful run tonight" in refusal(runs=skipped)
    # A producer that succeeded but observed none of its lanes was skipped all the same.
    silent = night()["ledger"]
    silent["cells"][2].update(result="skip", source_sha=None, image_digest=None, producer_source_sha=None,
                              started_at=None, completed_at=None, evidence_uri=None)
    assert "producer honua-sdk-python contributed no observation for its lanes" in refusal(ledger=silent)
    # A pass in a lane no producer ran tonight cannot be bound either.
    unowned = night()["ledger"]
    unowned["cells"][3].update(result="pass", source_sha=recorded["candidate"]["server_sha"])
    assert "from no producer dispatched tonight" in refusal(ledger=unowned)


def test_nightly_refuses_a_mismatched_source_sha():
    recorded = night()
    other = night()["ledger"]
    other["cells"][0]["source_sha"] = "b" * 40
    message = refusal(ledger=other)
    assert "source_sha " + "b" * 40 + " is not the candidate" in message
    producer = night()["ledger"]
    producer["cells"][2]["producer_source_sha"] = "7" * 40
    assert f"is not honua-sdk-python pin {'6' * 40}" in refusal(ledger=producer)
    moved = night()["runs"]
    moved[1]["head_sha"] = "7" * 40
    assert "ran at " + "7" * 40 + ", not its staged pin" in refusal(runs=moved)
    image = night()["ledger"]
    image["cells"][1]["image_digest"] = "sha256:" + "2" * 64
    assert "not about candidate" in refusal(ledger=image)
    wrong_requirements = night()["ledger"]
    wrong_requirements["requirements_source_revision"] = "8" * 40
    assert f"is not {recorded['requirements_revision']}" in refusal(ledger=wrong_requirements)
    missing_cell = night()["ledger"]
    missing_cell["cells"].pop()
    assert "cells do not match the staged catalog (4 cells for 5 requirements)" in refusal(ledger=missing_cell)


def test_nightly_stage_requires_a_pending_ledger(tmp_path):
    manifest = resolved_manifest()
    manifest["protocolCertification"]["ledger"]["status"] = "bound"
    with pytest.raises(MODULE.Finding, match="expects the resolved candidate's ledger to be pending"):
        MODULE.nightly_stage(candidate_root(tmp_path, manifest), None)


def test_nightly_convergence_is_deterministic(tmp_path):
    staged = []
    for name in ("first", "second"):
        root = fixture(tmp_path / name)
        align_components_to_published(root)
        plan, payloads, _ = MODULE.prepare(root, StubGitHub(root), "keep")
        staged.append((plan, payloads))
    assert staged[0][0] == staged[1][0]
    assert staged[0][1] == staged[1][1]
    catalog = json.loads(staged[0][1][str(MODULE.CATALOG)])
    # the expectation is the checked-in generated catalog, so a requirement row change cannot strand a literal
    generated = json.loads((ROOT / MODULE.CATALOG).read_text(encoding="utf-8"))
    assert catalog["production"]["cells"] == generated["production"]["cells"]
    assert sum(catalog["production"]["cells"].values()) == len(catalog["requirements"])

    recorded = night()
    bound = []
    for name in ("first", "second"):
        root = candidate_root(tmp_path / name)
        result = MODULE.nightly_bind(root, RecordedEvidence({recorded["evidence_commit"]: recorded["ledger"]}),
                                     recorded["requirements_revision"], recorded["runs"], "2026-10-03T12:50:00Z",
                                     recorded["dispatch_id"])
        bound.append(((root / MODULE.MANIFEST).read_bytes(), result))
    assert bound[0] == bound[1]


def test_nightly_bind_refuses_two_ledgers_for_one_staging(tmp_path):
    recorded = night()
    root = candidate_root(tmp_path)
    ledgers = {recorded["evidence_commit"]: recorded["ledger"], "3" * 40: recorded["ledger"]}
    with pytest.raises(MODULE.Finding, match="found 2"):
        MODULE.nightly_bind(root, RecordedEvidence(ledgers), recorded["requirements_revision"], recorded["runs"],
                            "2026-10-03T12:50:00Z", recorded["dispatch_id"])


PROGRAM = """app.MapServerFeatureEndpoints();
app.MapGrpcService<Honua.Server.Features.Protocols.Grpc.HonuaFeatureService>();
app.MapGrpcService<Honua.Geoprocessing.HonuaProcessService>();
if (CapabilityFlagOptions.IsExperimentalEnabled(builder.Configuration, "scene.catalog"))
{
    app.MapGrpcService<Honua.Scene.Grpc.HonuaSceneGrpcService>();
}
app.MapGrpcHealthChecksService();
"""
SERVICE_FILES = {
    "src/Honua.Server/Features/Protocols/Grpc/HonuaFeatureService.cs": "1" * 40,
    "src/Honua.Geoprocessing/Features/Geoprocessing/HonuaProcessService.cs": "2" * 40,
    "src/Honua.Scene/Grpc/HonuaSceneGrpcService.cs": "3" * 40,
    "src/Honua.Server/Program.cs": "4" * 40,
}
VERIFIED = "87966c3f7b6c840ffc4d4da0b451714ab717b18a"
NEXT_SERVER = "d1fc139a64ce33c817bd927bacb2103714221515"


class ServerTree:
    """honua-server Program.cs and tree listings per commit, as the contents and trees APIs return them."""

    def __init__(self, program=None, files=None):
        self.program = {NEXT_SERVER: program or PROGRAM}
        self.files = {NEXT_SERVER: files or SERVICE_FILES}

    def content(self, repository, path, revision):
        assert (repository, path) == ("honua-io/honua-server", "src/Honua.Server/Program.cs")
        return self.program.get(revision, PROGRAM).encode()

    def tree(self, repository, revision):
        return sorted(self.files.get(revision, SERVICE_FILES).items())


def grpc_root(tmp_path):
    root = tmp_path / "grpc"
    (root / MODULE.GRPC_OPERATIONS).parent.mkdir(parents=True)
    shutil.copy(ROOT / MODULE.GRPC_OPERATIONS, root / MODULE.GRPC_OPERATIONS)
    return root


def test_nightly_grpc_ruling_carries_only_to_an_identical_grpc_surface(tmp_path):
    root = grpc_root(tmp_path)
    before = (root / MODULE.GRPC_OPERATIONS).read_text(encoding="utf-8")

    record = MODULE.verify_grpc_scope(root, ServerTree(), NEXT_SERVER)

    assert record["server_commit"] == NEXT_SERVER and record["same_surface_as"] == VERIFIED
    after = (root / MODULE.GRPC_OPERATIONS).read_text(encoding="utf-8")
    ruling = next(r for r in json.loads(after)["rulings"] if r["id"] == MODULE.GRPC_RULING)
    assert ruling["verified_server_commits"][-2:] == [VERIFIED, NEXT_SERVER]
    # Only the new commit is inserted; the rest of the governed file is byte-identical.
    assert after.replace(f',\n                "{NEXT_SERVER}"', "", 1) == before
    # An already-verified server needs no re-check and changes nothing.
    assert MODULE.verify_grpc_scope(root, ServerTree(), NEXT_SERVER) is None
    assert (root / MODULE.GRPC_OPERATIONS).read_text(encoding="utf-8") == after


@pytest.mark.parametrize("change", ["unflagged-scene", "process-service-source"])
def test_nightly_grpc_ruling_refuses_a_changed_grpc_surface(tmp_path, change):
    root = grpc_root(tmp_path)
    before = (root / MODULE.GRPC_OPERATIONS).read_bytes()
    if change == "unflagged-scene":
        tree = ServerTree(program=PROGRAM.replace(
            'if (CapabilityFlagOptions.IsExperimentalEnabled(builder.Configuration, "scene.catalog"))\n{\n', "")
            .replace("    app.MapGrpcService<Honua.Scene.Grpc.HonuaSceneGrpcService>();\n}\n",
                     "app.MapGrpcService<Honua.Scene.Grpc.HonuaSceneGrpcService>();\n"))
    else:
        tree = ServerTree(files={**SERVICE_FILES,
                                 "src/Honua.Geoprocessing/Features/Geoprocessing/HonuaProcessService.cs": "9" * 40})
    with pytest.raises(MODULE.Finding, match="re-check which RPCs it implements"):
        MODULE.verify_grpc_scope(root, tree, NEXT_SERVER)
    assert (root / MODULE.GRPC_OPERATIONS).read_bytes() == before


def test_nightly_refuses_a_desktop_cell_whose_driver_is_not_its_requirement_or_receipt():
    # R40: client_driver and release_bucket are part of the staged requirement, and the receipt
    # binds the driver that produced it, so a rewritten driver is refused before the ledger binds.
    recorded = night()
    observed = next(index for index, cell in enumerate(recorded["ledger"]["cells"]) if cell["result"] == "pass")
    base = recorded["ledger"]["cells"][observed]
    [row] = [row for row in recorded["catalog"]["requirements"]
             if all(row.get(key) == base.get(key) for key in MODULE.LEDGER_IDENTITY[:7])]

    def staged(cell_driver, receipt_driver, requirement_driver="pyqgis"):
        catalog, ledger = night()["catalog"], night()["ledger"]
        target = next(item for item in catalog["requirements"]
                      if all(item.get(key) == row.get(key) for key in MODULE.LEDGER_IDENTITY[:7]))
        target.update(client_driver=requirement_driver, release_bucket="must-fix-before-cut")
        cell = ledger["cells"][observed]
        cell.update(client_driver=cell_driver, release_bucket="must-fix-before-cut")
        cell["evidence_receipt"]["identity"]["client_driver"] = receipt_driver
        rehash(cell)
        return ledger, catalog

    ledger, catalog = staged("qgis-ui", "qgis-ui")
    assert "cells do not match the staged catalog" in refusal(ledger=ledger, catalog=catalog)
    ledger, catalog = staged("pyqgis", "qgis-ui")
    message = refusal(ledger=ledger, catalog=catalog)
    assert "receipt driver 'qgis-ui' is not the cell driver 'pyqgis'" in message
    assert "cells do not match the staged catalog" not in message
