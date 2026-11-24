import hashlib
import json

import pytest

from check_native_ecs_qualification import CELL, PRODUCER, REPORT, FrozenCandidate, ProducerRun, Qualification, verify
from test_check_journey_receipts import MANIFEST, NOW, RUN, RUN_ATTEMPT, Intake

REPO = "honua-io/honua-release"
STACK = "arn:aws:cloudformation:us-east-1:123456789012:stack/honua-gate-fixture/abc-def"
SERVER = MANIFEST["components"]["honua-server"]
IMAGE = f"{SERVER['image']}@{SERVER['digest']}"


@pytest.fixture
def qualification(tmp_path):
    intake = Intake(tmp_path)
    root = intake.directory(CELL)
    run = {"id": int(RUN), "run_attempt": int(RUN_ATTEMPT), "head_branch": "trunk",
           "path": PRODUCER, "event": "workflow_dispatch", "status": "completed",
           "conclusion": "success", "repository": {"full_name": REPO},
           "head_repository": {"full_name": REPO}, "head_sha": "a" * 40,
           "run_started_at": "2026-10-02T19:55:00Z", "updated_at": "2026-10-02T20:00:00Z"}
    envelope = {"schemaVersion": 1, "status": "pass", "cell": CELL, "deployment": "cloudformation-ecs-fargate",
                "runId": RUN, "runAttempt": RUN_ATTEMPT, "candidateDigest": intake.digest,
                "serverImage": IMAGE, "lockDigest": "", "serverSha": SERVER["sha"],
                "imageDigest": SERVER["digest"],
                "journeyReportSha256": hashlib.sha256((root / REPORT).read_bytes()).hexdigest(),
                "stackId": STACK, "installedAt": "2026-10-02T19:58:00Z",
                "installation": {"stackName": "honua-gate-fixture", "declaredImage": IMAGE,
                                 "cpuArchitecture": "X86_64",
                                 "resolvedPlatformImageDigest": SERVER["platformDigests"]["amd64"],
                                 "observedPlatformImageDigest": SERVER["platformDigests"]["amd64"],
                                 "healthyTaskCount": 1,
                                 "startedAt": "2026-10-02T19:57:00+00:00",
                                 "completedAt": "2026-10-02T19:58:00+00:00"},
                "cleanup": {"stackId": STACK, "stackStatus": "DELETE_COMPLETE",
                            "remainingResources": [], "observedAt": "2026-10-02T19:59:59Z"}}
    return intake, run, envelope


def check(qualification, *, require_model=False):
    intake, run, envelope = qualification
    root = intake.directory(CELL)
    (root / "qualification.json").write_text(json.dumps(envelope))
    producer = ProducerRun(REPO, RUN, RUN_ATTEMPT)
    candidate = FrozenCandidate(intake.candidate, intake.digest, IMAGE, "")
    return verify(root, run, Qualification(producer, candidate, require_model), now=NOW)


def test_accepts_authenticated_installed_refusal_and_cleanup(qualification):
    journey, model = check(qualification)
    assert journey["status"] == "pass"
    assert journey["runId"] == RUN
    assert model is None


@pytest.mark.parametrize("field,value", [
    ("candidateDigest", "f" * 64),
    ("lockDigest", "sha256:" + "f" * 64),
    ("journeyReportSha256", "f" * 64),
    ("runAttempt", "1"),
])
def test_rejects_mismatched_envelope(qualification, field, value):
    qualification[2][field] = value
    with pytest.raises(ValueError, match="frozen candidate and journey report"):
        check(qualification)


@pytest.mark.parametrize("field,value", [
    ("path", ".github/workflows/release-train.yml"),
    ("head_repository", {"full_name": "fork/honua-release"}),
    ("run_attempt", 1),
    ("conclusion", "failure"),
])
def test_rejects_untrusted_run(qualification, field, value):
    qualification[1][field] = value
    with pytest.raises(ValueError, match="producer identity or conclusion"):
        check(qualification)


def test_rejects_unhealthy_installation(qualification):
    qualification[2]["installation"]["healthyTaskCount"] = 0
    with pytest.raises(ValueError, match="installation observation"):
        check(qualification)


def test_rejects_incomplete_cleanup(qualification):
    qualification[2]["cleanup"]["remainingResources"] = ["ServerService"]
    with pytest.raises(ValueError, match="cleanup"):
        check(qualification)


def test_rejects_journey_after_cleanup(qualification):
    qualification[2]["cleanup"]["observedAt"] = "2026-10-02T19:58:30Z"
    with pytest.raises(ValueError, match="outside the qualified stack lifetime"):
        check(qualification)


def test_rejects_missing_refusal_receipt(qualification):
    intake = qualification[0]
    next(intake.directory(CELL).rglob("serving-refusal-*.json")).unlink()
    with pytest.raises(ValueError, match="journey evidence|refusal"):
        check(qualification)


def test_missing_model_does_not_change_required_verdict(qualification):
    journey, model = check(qualification, require_model=True)
    assert journey["status"] == "pass"
    assert model["status"] == "fail"


def test_rejects_a_different_running_platform_digest(qualification):
    qualification[2]["installation"]["observedPlatformImageDigest"] = "sha256:" + "f" * 64
    with pytest.raises(ValueError, match="installation observation"):
        check(qualification)


def test_rejects_consistent_platform_digest_for_the_wrong_architecture(qualification):
    installation = qualification[2]["installation"]
    installation["resolvedPlatformImageDigest"] = SERVER["platformDigests"]["arm64"]
    installation["observedPlatformImageDigest"] = SERVER["platformDigests"]["arm64"]
    with pytest.raises(ValueError, match="installation observation"):
        check(qualification)
