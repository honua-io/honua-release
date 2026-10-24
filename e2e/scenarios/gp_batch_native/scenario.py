"""Native GDAL-worker Batch evidence for process.geoprocessing.

Only a completed native GDAL-worker OGC job is a pass. A managed geometry job,
a busybox image success, and a Redis-off refusal are not that evidence.

`evaluate(None)` stays blocked. It does not apply Terraform and it does not
start `--require-real`. A confirmed candidate run can pass a receipt into
`evaluate` later; this module never launches that run by itself.
"""
from __future__ import annotations

from runner.harness import Ctx
from runner.report import Result, Status

SCENARIO_ID = "gp_batch_native"

META = {
    "name": SCENARIO_ID,
    "seeded_from": "honua-release#85",
    "requires_server": False,
}

_BLOCKED = (
    "native GDAL-worker Batch proof was not supplied (honua-release#85); "
    "this scenario does not apply Terraform or start --require-real"
)


def evaluate(receipt: dict | None, *, redis_off: bool = False) -> dict:
    """Judge one receipt. Missing proof is blocked. Stand-ins fail. Only native GDAL passes."""
    if redis_off or (isinstance(receipt, dict) and receipt.get("reason") == "redis-off"):
        return {
            "scenario": SCENARIO_ID,
            "status": "fail",
            "why": "Redis-off refusal is not native GDAL-worker evidence",
        }
    if receipt is None:
        return {"scenario": SCENARIO_ID, "status": "blocked", "why": _BLOCKED}
    if not isinstance(receipt, dict):
        return {"scenario": SCENARIO_ID, "status": "fail", "why": "native GDAL receipt must be an object"}

    image = str(receipt.get("image") or "")
    if "busybox" in image.lower():
        return {"scenario": SCENARIO_ID, "status": "fail", "why": "busybox success is not native GDAL evidence"}

    job_type = str(receipt.get("jobType") or "")
    if job_type in {"managed-geometry", "managed_geometry"}:
        return {
            "scenario": SCENARIO_ID,
            "status": "fail",
            "why": "managed geometry job is not native GDAL-worker evidence",
        }

    artifact = receipt.get("artifact") if isinstance(receipt.get("artifact"), dict) else {}
    digest = str(artifact.get("sha256") or "")
    native = (
        receipt.get("status") == "successful"
        and receipt.get("workerKind") == "gdal-native"
        and str(receipt.get("engine") or "").lower() == "gdal"
        and receipt.get("api") == "ogc-processes"
        and "gdal" in image.lower()
        and isinstance(receipt.get("processId"), str)
        and bool(receipt.get("processId"))
        and bool(artifact.get("mediaType"))
        and isinstance(artifact.get("byteLength"), int)
        and artifact.get("byteLength", 0) > 0
        and len(digest) == 64
        and all(character in "0123456789abcdef" for character in digest)
    )
    if not native:
        return {
            "scenario": SCENARIO_ID,
            "status": "fail",
            "why": "receipt is not a completed native GDAL-worker OGC job",
        }
    return {
        "scenario": SCENARIO_ID,
        "status": "pass",
        "why": "native GDAL-worker OGC job completed",
        "evidence": {
            "processId": receipt["processId"],
            "workerKind": receipt["workerKind"],
            "image": image,
            "artifact": {
                "mediaType": artifact["mediaType"],
                "byteLength": artifact["byteLength"],
                "sha256": digest,
            },
        },
    }


def run(ctx: Ctx) -> Result:
    del ctx  # The live candidate run is a separate confirmation. Do not start it here.
    row = evaluate(None)
    return Result(
        scenario=META["name"],
        status=Status.BLOCKED,
        seeded_from=META["seeded_from"],
        why=row["why"],
        evidence={"admission": "blocked-until-native-receipt"},
    )
