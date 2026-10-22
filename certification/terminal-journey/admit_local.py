#!/usr/bin/env python3
"""Admit a committed local-docker journey receipt only when it is a live pass on the current pin.

Historical blocked receipts stay in fixtures/. This module does not create a pass, and a caller
must not treat an empty admission set as a failure of the gate.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OPERATION_STAGES = (3, 4, 6, 7, 8)
OPERATION_IDS = ("operationInstanceId", "correlationId", "auditId")
JOB_IDS = ("jobId", "resourceUri", "jobStatus", "jobCreatedAt")


def server_image_ref(component: dict[str, Any]) -> str:
    return f"{component['image']}@{component['digest']}"


def admission_gaps(receipt: dict[str, Any], *, server_sha: str, server_image: str) -> list[str]:
    """Reasons this document is not the committed local stages 1–8 pass. Empty means admitted."""
    gaps: list[str] = []
    if not isinstance(receipt, dict):
        return ["receipt is not an object"]
    if receipt.get("receiptSchema") != "terminal-journey-receipt-v1":
        gaps.append("receiptSchema is not the terminal journey receipt")
    if receipt.get("status") != "pass" or receipt.get("mode") != "live":
        gaps.append("overall status is not a live pass")
    target = receipt.get("target") if isinstance(receipt.get("target"), dict) else {}
    if target.get("id") != "local-docker" or target.get("kind") != "local-docker":
        gaps.append("target is not local-docker")
    roster = receipt.get("roster") if isinstance(receipt.get("roster"), dict) else {}
    workspace = receipt.get("clientWorkspace") if isinstance(receipt.get("clientWorkspace"), dict) else {}
    if roster.get("status") != "pass":
        gaps.append("roster is not pass")
    if workspace.get("status") != "pass":
        gaps.append("client workspace is not pass")
    server = receipt.get("server") if isinstance(receipt.get("server"), dict) else {}
    if server.get("sourceSha") != server_sha or server.get("image") != server_image:
        gaps.append("server pin does not match the manifest")
    stages = receipt.get("stages")
    if not isinstance(stages, list) or [row.get("number") for row in stages if isinstance(row, dict)] != list(range(1, 9)):
        gaps.append("stages 1-8 are not all present")
        return gaps
    by_number = {row["number"]: row for row in stages}
    for number, row in by_number.items():
        if row.get("status") != "pass":
            gaps.append(f"stage {number} is not pass")
    for number in OPERATION_STAGES:
        row = by_number[number]
        if row.get("topologyRefusal"):
            if number in (7, 8):
                gaps.append(f"stage {number} is a topology refusal, not an observed operation")
            continue
        for key in OPERATION_IDS:
            if not row.get(key):
                gaps.append(f"stage {number} lacks {key}")
    stage_five = by_number[5]
    if not stage_five.get("topologyRefusal"):
        for key in JOB_IDS:
            if not stage_five.get(key):
                gaps.append(f"stage 5 lacks {key}")
    for number in (7, 8):
        if not by_number[number].get("proposalId"):
            gaps.append(f"stage {number} lacks proposalId")
    approval = receipt.get("approval") if isinstance(receipt.get("approval"), dict) else {}
    stage_eight = by_number[8]
    if (not approval.get("proposalId") or not approval.get("resolvedBy") or not approval.get("auditId")
            or approval.get("proposalId") != stage_eight.get("proposalId")
            or approval.get("auditId") != stage_eight.get("auditId")):
        gaps.append("approval does not record proposalId, resolvedBy and the stage 8 audit id")
    return gaps


def admits(receipt: dict[str, Any], *, server_sha: str, server_image: str) -> bool:
    return not admission_gaps(receipt, server_sha=server_sha, server_image=server_image)


def scan_committed(directory: Path, *, server_sha: str, server_image: str) -> list[Path]:
    """Committed local-docker receipts in directory that this gate admits. May be empty."""
    admitted: list[Path] = []
    for path in sorted(directory.glob("*.local-docker.json")):
        document = json.loads(path.read_text(encoding="utf-8"))
        if admits(document, server_sha=server_sha, server_image=server_image):
            admitted.append(path)
    return admitted
