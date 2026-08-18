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

CANDIDATE IDENTITY IS PART OF THE VERDICT. An error budget is only the candidate's if it was
scraped FROM the candidate. Honua is not a SaaS: the one long-lived deployed environment
(demo.honua.io, owned by honua-io/honua-demo-infra) doubles as demo and certification target, and it
routinely runs a build older than the manifest pin. Pointing HONUA_METRICS_URL at an instance
running a different commit would compute a real, correct-looking ratio over the WRONG population —
the same class of defect as the fail-open denominator this gate already fixed once (honua-release#5).
So the gate first proves the scraped instance IS the pinned candidate, by comparing honua-server's
advertised build identity against `components.honua-server.sha` in platform-manifest.yaml:

  GET /api/v1/capabilities/manifest        -> $.server.deploymentRevision (+ .deploymentRevisionSource)
  GET /api/v1/streaming/features/capabilities -> $.data.deploymentRevision (same fields, envelope shape)

Both are public (unlike /metrics, which honua-server maps with .RequireAuthorization("Admin")).
A mismatched revision, an unreadable one, or an absent one is `blocked` — never `pass`, and under
strict enforcement a hard FAIL. There is no "assume it matches" path: `evaluate_gate` refuses to
even consult the error budget until identity is confirmed.

The decision logic is pure + unit-tested here (this module makes no network calls); the workflow
does the scraping and the capability fetch and feeds the values in. Until a candidate is deployed
with a scrapeable HONUA_METRICS_URL, the workflow reports BLOCKED — never a fake green.

  evaluate_slo(error_total, request_total, max_error_rate) -> (status, why)
  evaluate_candidate_identity(instance_revision, pinned_sha, revision_source) -> (status, why)
  evaluate_gate(...) -> (status, why)          # identity first, budget second
  python tools/check_slo.py --error-total N --request-total M --pinned-sha SHA \
      --instance-revision REV [--revision-source commit-sha] [--max-error-rate 0.01]
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.parse

# The public endpoint carrying honua-server's build identity. /metrics is Admin-authorized; this is
# not, so identity can be established even when the scrape credential is missing.
CAPABILITY_MANIFEST_PATH = "/api/v1/capabilities/manifest"

# The only `deploymentRevisionSource` value that is comparable to a manifest `sha`. Anything else
# (a build number, a chart version, an image tag) is not a commit and must not be compared to one.
REVISION_SOURCE_COMMIT_SHA = "commit-sha"

# Git commit digests, abbreviated or full. The prefix rule below only ever compares hex of >= 7
# characters, so an empty or malformed value can never "match" a pin by accident.
_COMMIT_SHA = re.compile(r"[0-9a-f]{7,64}")


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


def capability_manifest_url(metrics_url: str | None) -> str | None:
    """Derive the candidate's public capability-manifest URL from its /metrics URL.

    Same origin by construction: the instance whose identity we check must be the instance we
    scraped, so the URL is never taken from a separate variable that could drift to a different host.
    Returns None when there is no usable metrics URL (nothing deployed / nothing configured).
    """
    parts = urllib.parse.urlsplit((metrics_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, CAPABILITY_MANIFEST_PATH, "", ""))


def read_instance_revision(document: object) -> tuple[str | None, str | None]:
    """Pull (deploymentRevision, deploymentRevisionSource) out of a capability response.

    honua-server advertises the pair in two shapes, both verified live against demo.honua.io:
      * /api/v1/capabilities/manifest         -> {"server": {"deploymentRevision": ..., ...}}
      * /api/v1/streaming/features/capabilities -> {"success": true, "data": {"deploymentRevision": ...}}
    The top level is also accepted so a future unwrapped response does not silently read as "absent"
    (absent is a BLOCKING outcome, so a shape miss is loud, not fail-open — but it is still a false
    block, and this gate has enough real reasons to block).

    Returns (None, None) when no revision is present. The caller decides what that means; there is
    deliberately no "assume it matches" default here.
    """
    if not isinstance(document, dict):
        return None, None
    for container in (document.get("server"), document.get("data"), document):
        if not isinstance(container, dict):
            continue
        revision = container.get("deploymentRevision")
        if isinstance(revision, str) and revision.strip():
            source = container.get("deploymentRevisionSource")
            return revision.strip(), source.strip() if isinstance(source, str) and source.strip() else None
    return None, None


def evaluate_candidate_identity(instance_revision: str | None, pinned_sha: str | None,
                                revision_source: str | None = None) -> tuple[str, str]:
    """pass | blocked — is the scraped instance the manifest-pinned candidate?

    There is no `fail` verdict: a mismatch is not a defect in the candidate, it is the absence of any
    evidence ABOUT the candidate. The gate's enforcement policy already turns `blocked` into a hard
    FAIL under strict, which is the required outcome; what must never happen is `pass`.

    Abbreviated revisions are honoured (a deploy may advertise a short sha while the manifest pins the
    full one), but only as a hex prefix of >= 7 characters in one direction or the other. Every other
    outcome — no pin, no revision, a non-commit revision source, a non-hex value, a genuine
    disagreement — blocks and names what it saw.
    """
    pinned = (pinned_sha or "").strip().lower()
    if not pinned:
        return "blocked", ("pinned candidate sha unavailable — components.honua-server.sha could not be "
                           "read from platform-manifest.yaml, so there is nothing to bind the scrape to")
    if not _COMMIT_SHA.fullmatch(pinned):
        return "blocked", (f"pinned candidate sha {pinned!r} is not a commit digest — refusing to treat "
                           "any scraped instance as the candidate")

    revision = (instance_revision or "").strip().lower()
    if not revision:
        return "blocked", (f"scraped instance advertised no build identity (no deploymentRevision read "
                           f"from {CAPABILITY_MANIFEST_PATH} — no candidate deployed, endpoint "
                           f"unreachable, or the field is absent), so it cannot be shown to be pinned "
                           f"candidate honua-server.sha={pinned}; an unidentified instance is never "
                           "assumed to match")

    source = (revision_source or "").strip().lower()
    # An ABSENT source is tolerated: the comparison against a 40-char pin is itself the proof, and
    # older candidates predate the field. A source that is present and is NOT a commit sha is not.
    if source and source != REVISION_SOURCE_COMMIT_SHA:
        return "blocked", (f"scraped instance advertises deploymentRevisionSource={source!r}, not "
                           f"{REVISION_SOURCE_COMMIT_SHA!r} — its deploymentRevision={revision!r} is not "
                           f"comparable to pinned candidate honua-server.sha={pinned}")
    if not _COMMIT_SHA.fullmatch(revision):
        return "blocked", (f"scraped instance deploymentRevision={revision!r} is not a commit digest — "
                           f"cannot bind it to pinned candidate honua-server.sha={pinned}")

    if not (revision.startswith(pinned) or pinned.startswith(revision)):
        return "blocked", (f"scraped instance is NOT the pinned candidate: instance "
                           f"deploymentRevision={revision} vs manifest honua-server.sha={pinned} — an "
                           "error budget measured here belongs to a different build")

    return "pass", (f"scraped instance deploymentRevision={revision} matches pinned candidate "
                    f"honua-server.sha={pinned}")


def evaluate_gate(error_total: float | None, request_total: float | None,
                  max_error_rate: float = 0.01, *, instance_revision: str | None = None,
                  pinned_sha: str | None = None,
                  revision_source: str | None = None) -> tuple[str, str]:
    """The gate verdict: candidate identity first, error budget second.

    Identity is a precondition, not a co-equal signal, so it short-circuits. That ordering is what
    makes `pass` unreachable for an instance whose identity was not confirmed — the budget is never
    even consulted.
    """
    identity_status, identity_why = evaluate_candidate_identity(instance_revision, pinned_sha, revision_source)
    if identity_status != "pass":
        return "blocked", f"candidate identity unconfirmed — {identity_why}"
    status, why = evaluate_slo(error_total, request_total, max_error_rate)
    return status, f"{why} [candidate identity: {identity_why}]"


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
    # Candidate identity binding. Both default to empty rather than being `required`, because an
    # empty value is a MEANINGFUL input here — it is exactly the "nothing deployed / nothing readable"
    # case, and it blocks. Omitting them cannot produce a pass.
    ap.add_argument("--pinned-sha", default="",
                    help="components.honua-server.sha from platform-manifest.yaml (empty = unreadable)")
    ap.add_argument("--instance-revision", default="",
                    help="deploymentRevision advertised by the scraped instance (empty = absent)")
    ap.add_argument("--revision-source", default="",
                    help="deploymentRevisionSource advertised by the scraped instance")
    ap.add_argument("--require-real", action="store_true", help="promote BLOCKED to FAIL")
    args = ap.parse_args(argv)

    status, why = evaluate_gate(_opt_float(args.error_total), _opt_float(args.request_total),
                                args.max_error_rate,
                                instance_revision=args.instance_revision,
                                pinned_sha=args.pinned_sha,
                                revision_source=args.revision_source)
    print(f"status={status}")
    print(f"why={why}")
    if status == "fail":
        return 1
    if status == "blocked" and args.require_real:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
