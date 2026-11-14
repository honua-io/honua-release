#!/usr/bin/env python3
"""Create the RC release asset from the exact candidate admitted by the gate window.

This records existing admission; it never certifies a different image or finalizes
an RC as GA. The published gate-window.json retains the complete gate evidence.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ReleaseInputs:
    label: str
    image: str
    platform_lock: str


def _admitted_candidate(report: dict, platform_lock: str) -> dict:
    if report.get("admissible") is not True:
        raise ValueError("the gate window has not admitted this candidate")
    candidate = report.get("candidate") or {}
    patterns = {"server_sha": r"[0-9a-f]{40}",
                "server_digest": r"sha256:[0-9a-f]{64}",
                "platform_lock": r"sha256:[0-9a-f]{64}"}
    for field, pattern in patterns.items():
        value = candidate.get(field)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"gate window candidate has invalid {field}")
    if candidate["platform_lock"] != platform_lock:
        raise ValueError("gate window candidate does not match the requested platform lock")
    return candidate


def snapshot(manifest: dict, report: dict, inputs: ReleaseInputs) -> dict:
    """Preserve component metadata only when it describes the admitted image."""
    if not re.fullmatch(r"[0-9]{4}\.[0-9]+-rc\.[1-9][0-9]*", inputs.label):
        raise ValueError("release label must be YYYY.N-rc.N")
    if not re.fullmatch(r"ghcr\.io/honua-io/honua-server:[\w][\w.-]{0,127}", inputs.image, re.ASCII):
        raise ValueError("image must name a tagged honua-server GHCR image")
    candidate = _admitted_candidate(report, inputs.platform_lock)
    server = manifest["components"]["honua-server"]
    if (server.get("sha") != candidate["server_sha"]
            or server.get("digest") != candidate["server_digest"]
            or manifest.get("candidate", {}).get("ref") != candidate["server_sha"]):
        raise ValueError("manifest candidate must match the admitted source SHA and image digest")
    result = copy.deepcopy(manifest)
    result["platformRelease"] = inputs.label
    result["status"] = "rc"
    result["components"]["honua-server"]["image"] = inputs.image
    result["releaseAdmission"] = copy.deepcopy(candidate)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="platform-manifest.yaml")
    parser.add_argument("--gate-window", default="gate-window.json")
    parser.add_argument("--label", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--platform-lock", required=True)
    parser.add_argument("--out", default="finalized-manifest.yaml")
    args = parser.parse_args(argv)
    try:
        manifest = yaml.safe_load(Path(args.manifest).read_text(encoding="utf-8"))
        report = json.loads(Path(args.gate_window).read_text(encoding="utf-8"))
        result = snapshot(manifest, report, ReleaseInputs(args.label, args.image, args.platform_lock))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
        print(f"RC manifest refused: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(yaml.safe_dump(result, sort_keys=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
