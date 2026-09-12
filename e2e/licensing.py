#!/usr/bin/env python3
"""Fail-closed runtime check for the 2026.1 disabled-licensing contract."""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path


def validate_disabled(document: object) -> None:
    data = document.get("data") if isinstance(document, dict) else None
    if not isinstance(data, dict) or data.get("mode") != "disabled":
        raise ValueError("2026.1 requires admin license mode: disabled (missing/enabled is non-passing)")


def assert_disabled(base_url: str, admin_key: str | None = None) -> dict:
    key = admin_key if admin_key is not None else os.environ.get("HONUA_ADMIN_PASSWORD", "honua-console-dev-key")
    request = urllib.request.Request(base_url.rstrip("/") + "/api/v1/admin/license",
                                     headers={"X-API-Key": key})
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise ValueError(f"admin license status returned HTTP {response.status}")
        validate_disabled(json.load(response))
    # Only record the asserted public fact; never serialize credentials or a license envelope.
    return {"mode": "disabled", "status": "pass", "surface": "/api/v1/admin/license"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        receipt = assert_disabled(args.base_url)
    except Exception as exc:
        print(f"licensing-disabled: FAIL: {exc}")
        return 1
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print("licensing-disabled: PASS mode: disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
