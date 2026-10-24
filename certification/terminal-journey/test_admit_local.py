"""The local stages 1–8 gate admits only a live pass on the current server pin."""
import json
from pathlib import Path

import yaml

import admit_local

HERE = Path(__file__).parent
ROOT = HERE.parents[1]


def _manifest_pin():
    manifest = yaml.safe_load((ROOT / "platform-manifest.yaml").read_text(encoding="utf-8"))
    server = manifest["components"]["honua-server"]
    return server["sha"], admit_local.server_image_ref(server)


def _pass_receipt(sha, image):
    def stage(number):
        row = {
            "number": number,
            "status": "pass",
            "proposalId": "proposal-1" if number in (7, 8) else None,
            "auditId": "audit-8" if number == 8 else "audit",
            "operationInstanceId": "opinst",
            "correlationId": "corr",
            "jobId": "job-1",
            "resourceUri": "honua://jobs/job-1",
            "jobStatus": "accepted",
            "jobCreatedAt": "2026-10-21T00:00:00Z",
        }
        return row

    return {
        "receiptSchema": "terminal-journey-receipt-v1",
        "status": "pass",
        "mode": "live",
        "target": {"id": "local-docker", "kind": "local-docker"},
        "roster": {"status": "pass"},
        "clientWorkspace": {"status": "pass"},
        "server": {"sourceSha": sha, "image": image},
        "stages": [stage(number) for number in range(1, 9)],
        "approval": {"proposalId": "proposal-1", "resolvedBy": "actor-approver", "auditId": "audit-8"},
    }


def test_a_current_pin_live_pass_is_admitted_and_a_weaker_receipt_is_not():
    sha, image = _manifest_pin()
    receipt = _pass_receipt(sha, image)
    assert admit_local.admits(receipt, server_sha=sha, server_image=image)
    blocked = {**receipt, "status": "blocked"}
    assert not admit_local.admits(blocked, server_sha=sha, server_image=image)
    stale = json.loads(json.dumps(receipt))
    stale["server"]["sourceSha"] = "0" * 40
    assert any("server pin" in gap for gap in admit_local.admission_gaps(stale, server_sha=sha, server_image=image))
    missing = json.loads(json.dumps(receipt))
    del missing["approval"]["resolvedBy"]
    assert any("approval" in gap for gap in admit_local.admission_gaps(missing, server_sha=sha, server_image=image))


def test_committed_fixtures_are_not_required_to_contain_a_pass():
    sha, image = _manifest_pin()
    fixtures = HERE / "fixtures"
    admitted = admit_local.scan_committed(fixtures, server_sha=sha, server_image=image)
    for path in admitted:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert admit_local.admits(document, server_sha=sha, server_image=image)
    smoke = fixtures / "smoke-receipt.local-docker.json"
    assert smoke.exists()
    assert smoke not in admitted
