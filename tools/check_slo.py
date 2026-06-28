#!/usr/bin/env python3
"""SLO / error-budget gate (gate g) — evaluate the GeoServices in-band error rate against a budget.

The audit found the platform blind to its own error rate: GeoServices returns errors as HTTP 200
(server#2243), and the SLO release gate green-lit failing releases (devops#113). This gate makes the
SLO decision *mechanical and able to fail*: given the in-band error counter
(`honua_geoservices_error_total`) and the total request counter scraped from a deployed candidate,
the error rate must be within budget — else the gate is red.

The decision logic is pure + unit-tested here; the workflow scrapes the metrics and feeds them in.
Until the server actually emits the in-band error metric (issue #5) and a staging candidate is
deployed, the workflow reports BLOCKED — never a fake green.

  evaluate_slo(error_total, request_total, max_error_rate) -> (status, why)
  python tools/check_slo.py --error-total N --request-total M [--max-error-rate 0.01]
"""
from __future__ import annotations

import argparse
import sys


def evaluate_slo(error_total: float | None, request_total: float | None,
                 max_error_rate: float = 0.01) -> tuple[str, str]:
    """pass | fail | blocked.

    blocked — the metric is absent (None): the in-band error metric isn't wired (the very blindness
              server#2243 is about) or no candidate is deployed; we refuse to call that a pass.
    fail    — error_rate > budget (the release is breaching its error budget).
    pass    — within budget.
    """
    if error_total is None or request_total is None:
        return "blocked", ("honua_geoservices_error_total / request total not exposed — in-band error "
                           "metric not wired (server#2243) or no candidate deployed")
    if request_total <= 0:
        return "blocked", "no requests observed on the candidate (request_total=0) — cannot evaluate SLO"
    rate = error_total / request_total
    if rate > max_error_rate:
        return "fail", f"error rate {rate:.4f} exceeds budget {max_error_rate:.4f} ({int(error_total)}/{int(request_total)})"
    return "pass", f"error rate {rate:.4f} within budget {max_error_rate:.4f} ({int(error_total)}/{int(request_total)})"


def _opt_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--error-total", default=None, help="honua_geoservices_error_total sample (empty = absent)")
    ap.add_argument("--request-total", default=None, help="total request counter sample (empty = absent)")
    ap.add_argument("--max-error-rate", type=float, default=0.01)
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL")
    args = ap.parse_args(argv)

    status, why = evaluate_slo(_opt_float(args.error_total), _opt_float(args.request_total), args.max_error_rate)
    print(f"status={status}")
    print(f"why={why}")
    if status == "fail":
        return 1
    if status == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
