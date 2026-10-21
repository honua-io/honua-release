"""Read-only atomic job/result observation for an owned planning journey."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_map_package_validation import strict_json
from mcp_planning_disclosure import scenario
from mcp_render_validation import inspect_candidate
from mcp_execute_plan_state import snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--canonical-client", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        authored = scenario(args.fixture.read_bytes(), strict_json(args.bootstrap.read_bytes()), args.nonce, args.canonical_client)
        before = inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"])
        if before != authored["runtimeBinding"]:
            raise ValueError("planning store belongs to another runtime")
        started = datetime.now(timezone.utc).isoformat()
        value = snapshot(authored["handoffPlan"]["planId"])
        completed = datetime.now(timezone.utc).isoformat()
        if inspect_candidate(authored["candidateSourceSha"], authored["candidateImage"]) != before:
            raise ValueError("planning runtime changed during atomic store read")
        print(json.dumps({"schema": "honua.mcp-planning-store/v1", "runtimeBinding": before,
                          "startedAt": started, "completedAt": completed, "value": value}, allow_nan=False))
    except (ValueError, KeyError, TypeError, OSError, OverflowError):
        print("owned planning store observation refused", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
