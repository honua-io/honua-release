#!/usr/bin/env python3
"""Re-drive teardown for one run-scoped cloud parity cell after cancellation or interruption."""
from __future__ import annotations

import argparse
import os

from targets import REGISTRY


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(REGISTRY))
    parser.add_argument("--redis", required=True, choices=("on", "off"))
    args = parser.parse_args()

    target = REGISTRY[args.target](run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    target.teardown(redis_enabled=args.redis == "on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
