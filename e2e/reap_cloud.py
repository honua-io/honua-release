#!/usr/bin/env python3
"""Re-drive teardown for one run-scoped cloud parity cell after cancellation or interruption.

This is the backstop behind `run_cloud.py`'s own teardown: the workflow runs it whenever a cell
attempted to provision, including after a cancellation that killed the process mid-apply. It is
FAIL-CLOSED — if the cell's infrastructure cannot be destroyed, the step goes red rather than
quietly leaving an orphaned VPC/cluster billing (honua-iac#142).

The one transient it retries is Terraform state-lock contention: a cancelled `terraform apply` can
still hold the lock for a few seconds after this process starts.
"""
from __future__ import annotations

import argparse
import os
import time

from targets import REGISTRY
from targets.base import ProvisionError

REAP_ATTEMPTS = 12
REAP_DELAY_SECONDS = 5.0


def _is_state_lock_error(error: ProvisionError) -> bool:
    detail = str(error).casefold()
    return "state lock" in detail and ("acquir" in detail or "locked" in detail)


def reap(target, *, redis_enabled: bool, sleep=time.sleep) -> None:
    """Retry only transient Terraform state-lock contention; fail closed on anything else."""
    for attempt in range(1, REAP_ATTEMPTS + 1):
        try:
            target.teardown(redis_enabled=redis_enabled)
            return
        except ProvisionError as error:
            if not _is_state_lock_error(error) or attempt == REAP_ATTEMPTS:
                raise
            print(
                f"::warning title=cloud teardown state lock::retrying cleanup "
                f"({attempt}/{REAP_ATTEMPTS}) after the interrupted Terraform process exits"
            )
            sleep(REAP_DELAY_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=sorted(REGISTRY))
    parser.add_argument("--redis", required=True, choices=("on", "off"))
    args = parser.parse_args()

    target = REGISTRY[args.target](run_id=os.environ.get("GITHUB_RUN_ID", "local"))
    reap(target, redis_enabled=args.redis == "on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
