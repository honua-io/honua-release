#!/usr/bin/env python3
"""SLO / error-budget gate (gate g) — evaluate the GeoServices in-band error rate against a budget.

The audit found the platform blind to its own error rate: GeoServices returns errors as HTTP 200
(server#2243), and the SLO release gate green-lit failing releases (devops#113). This gate makes the
SLO decision *mechanical and able to fail*: given the in-band error counter
(`honua_geoservices_error_total`) and the request total scraped from a deployed candidate,
the error rate must be within budget — else the gate is red.

The request total comes from `honua_serving_request_duration_ms_count`, the Prometheus _count child
of the server's serving-plane latency histogram — one sample per served request that classifies to a
Honua protocol, and the only request-volume series honua-server exports. The gate previously
defaulted to `honua_geoservices_requests_total`, which no Honua component has ever emitted; since
`evaluate_slo` returns "blocked" when request_total is None, that made this gate structurally
incapable of ever returning `pass` (honua-release#5). The names are now guarded across the repo
seam by tools/check_metric_contract.py.

The decision logic is pure + unit-tested here; the workflow scrapes the metrics and feeds them in.
Until a staging candidate is deployed with a scrapeable HONUA_METRICS_URL, the workflow reports
BLOCKED — never a fake green.

  evaluate_slo(error_total, request_total, max_error_rate) -> (status, why)
  python tools/check_slo.py --error-total N --request-total M [--max-error-rate 0.01]
"""
from __future__ import annotations

import argparse
import sys


def evaluate_slo(error_total: float | None, request_total: float | None,
                 max_error_rate: float = 0.01) -> tuple[str, str]:
    """pass | fail | blocked.

    blocked — no usable denominator: the request total is absent (no candidate deployed, nothing
              scrapeable, or no in-scope traffic) or it is zero. We refuse to call that a pass.
    fail    — error_rate > budget (the release is breaching its error budget).
    pass    — within budget.

    An absent ERROR total with a live denominator means zero errors, not "unknown". OpenTelemetry
    does not export a counter series until it takes its first measurement, so a candidate that has
    served real traffic without producing a single error envelope exposes no
    honua_geoservices_error_total at all. Treating that as `blocked` made the gate unable to pass on
    exactly the releases it should wave through — and under strict enforcement it turned a clean
    candidate into a hard failure. The denominator is what proves the candidate is real and serving;
    once it is present, an absent numerator is a genuine zero (honua-release#5).
    """
    if request_total is None:
        return "blocked", ("request total not exposed — no candidate deployed, nothing scrapeable, "
                           "or no in-scope traffic on the candidate")
    if request_total <= 0:
        return "blocked", "no requests observed on the candidate (request_total=0) — cannot evaluate SLO"
    if error_total is None:
        return "pass", (f"no error envelopes exported against {int(request_total)} in-scope requests — "
                        "OpenTelemetry does not export a counter before its first measurement, so an "
                        "absent error series with a live request series is zero errors")
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
