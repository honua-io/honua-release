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
defaulted to `honua_geoservices_requests_total`, which no Honua component has ever emitted; an absent
denominator blocks, so that made this gate structurally incapable of ever returning `pass`
(honua-release#5). The names are now guarded across the repo seam by tools/check_metric_contract.py.

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

THE MEASUREMENT IS A WINDOW, NOT A COUNTER READING. `honua_geoservices_error_total` and
`honua_serving_request_duration_ms_count` are cumulative counters scoped to one server process's
lifetime, and honua-server runs on Lambda. Reading them raw made this gate's verdict a property of
traffic TIMING rather than of the candidate: measured against the same unchanged candidate on
2026-08-18 the gate returned `fail` (17.6%, poisoned by six bad requests an operator had issued
minutes earlier and which no volume of clean traffic can ever dilute), then `blocked` (the container
recycled and both series vanished), then `pass`, then `skipped` — four verdicts in an hour with no
release change. A counter has no time window, a `/metrics` read samples ONE container out of a
fleet, and a cold container reports an empty denominator.

So the gate measures a DELTA over a window it creates itself: scrape, drive a bounded deterministic
burst of ordinary in-scope GeoServices reads against the candidate, scrape again, and evaluate the
difference. Historical counter state cancels out, the denominator is guaranteed non-empty, and the
window is the same size on every run — which is what makes consecutive runs agree.

The delta is only meaningful if BOTH scrapes and the burst hit the same server process, and on
Lambda that is not guaranteed. It is therefore CHECKED, not assumed, and a failed check is `blocked`:

  * process continuity — per-process cumulative counters (CPU seconds, GC allocations, thrown
    exceptions...) must be non-decreasing across the two scrapes. Any decrease proves a different
    process answered, so the delta is meaningless.
  * probe attribution — the scraped process must have observed at least the whole burst
    (delta_request >= probe_requests). Fewer means the burst was spread across the fleet.
  * budget resolution — the window must be large enough that a single error is inside the budget's
    resolution (delta_request >= 1/max_error_rate). A 5-request window cannot "pass" a 1% budget.

A negative delta, a series that vanished, or any failed check is `blocked` — never `pass`, and never
clamped to zero.

  counter_delta(before, after) -> (delta|None, why|None)
  evaluate_process_continuity(before_witness, after_witness) -> (status, why)
  evaluate_slo_window(...) -> (status, why)      # + unrated_errors, reported never rated
  evaluate_candidate_identity(instance_revision, pinned_sha, revision_source) -> (status, why)
  evaluate_gate(...) -> (status, why)          # identity first, window second
  python tools/check_slo.py --error-before N --error-after N --request-before M --request-after M \
      --probe-requests K --continuity ok|<why it is not> --pinned-sha SHA \
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


# Per-process cumulative counters used as a CONTINUITY WITNESS: proof that the two scrapes bracketing
# the window were answered by the same server process. Every one of these is monotonic for the life of
# a process and resets to zero in a new one, so a DECREASE is positive proof that a different container
# answered the second scrape — at which point the delta between them is arithmetic on two unrelated
# populations. Verified present in the live demo.honua.io exposition (native AOT, so JIT counters are
# deliberately NOT in this set: they sit at 0 and witness nothing).
CONTINUITY_WITNESS_SERIES = (
    "process_cpu_time_seconds_total",
    "dotnet_gc_heap_total_allocated_bytes_total",
    "dotnet_gc_collections_total",
    "dotnet_exceptions_total",
    "dotnet_thread_pool_work_item_count_total",
    "aspnetcore_routing_match_attempts_total",
)

# How many witness series must be comparable across the two scrapes before continuity is credited.
# One series could be coincidentally non-decreasing on a fresh container; requiring several makes an
# accidental "same process" reading much harder than a real one.
MIN_CONTINUITY_WITNESSES = 3


def counter_delta(before: float | None, after: float | None) -> tuple[float | None, str | None]:
    """Difference of a cumulative counter across a window. Returns (delta, None) or (None, why).

    `None` for a sample means the series was ABSENT from that scrape, which is not the same as zero:
    OpenTelemetry does not export a counter before its first measurement.

      absent -> absent   : 0.0   (never measured in this window)
      absent -> present  : the after value (the counter took its first measurement inside the window)
      present -> absent  : INVALID — a counter that existed cannot un-exist on the same process
      present -> present : after - before, and a NEGATIVE result is INVALID, not clamped. A counter
                           going backwards means a different process answered; clamping it to zero
                           would silently convert "I measured two unrelated containers" into "no
                           errors", which is the fail-open shape this gate exists to prevent.
    """
    if after is None:
        if before is None:
            return 0.0, None
        return None, (f"series was present at {before:g} before the window and absent after it — a "
                      "cumulative counter cannot un-exist on a live process, so the two scrapes did "
                      "not observe the same instance")
    if before is None:
        return float(after), None
    delta = float(after) - float(before)
    if delta < 0:
        return None, (f"series went BACKWARDS across the window ({before:g} -> {after:g}) — cumulative "
                      "counters only decrease when the process restarts or a different instance "
                      "answered, so the difference is not a rate")
    return delta, None


def evaluate_process_continuity(before: dict[str, float | None],
                                after: dict[str, float | None]) -> tuple[str, str]:
    """pass | blocked — did the same server process answer both scrapes bracketing the window?

    On Lambda a `/metrics` read lands on an arbitrary container, so a delta between two reads is only
    a rate if both reads came from one process. The witness is CONTINUITY_WITNESS_SERIES: cumulative
    per-process counters that reset in a new container. Any decrease is proof of a different process.

    This does not PROVE sameness (a fresh container could coincidentally be higher on every series);
    it is a one-sided check that catches the recycle case, and it is paired with the probe-attribution
    floor in evaluate_slo_window, which a different container cannot satisfy without having served the
    whole burst itself.
    """
    comparable, regressed = [], []
    for name in CONTINUITY_WITNESS_SERIES:
        b, a = before.get(name), after.get(name)
        if b is None or a is None:
            # Absent before => the counter started inside the window, which is normal and witnesses
            # nothing either way. Absent after but present before is caught as a regression below.
            if b is not None and a is None:
                regressed.append(f"{name} present at {b:g} then absent")
            continue
        comparable.append(name)
        if a < b:
            regressed.append(f"{name} {b:g} -> {a:g}")
    if regressed:
        return "blocked", ("the two scrapes bracketing the window were answered by DIFFERENT instances "
                           f"— per-process counters went backwards: {'; '.join(sorted(regressed))}")
    if len(comparable) < MIN_CONTINUITY_WITNESSES:
        return "blocked", (f"only {len(comparable)} of {len(CONTINUITY_WITNESS_SERIES)} continuity "
                           f"witness series were comparable across the window (need "
                           f"{MIN_CONTINUITY_WITNESSES}) — cannot show both scrapes came from one "
                           "instance, so their difference is not a rate")
    return "pass", f"{len(comparable)} per-process counters non-decreasing across the window"


def minimum_resolvable_window(max_error_rate: float) -> int:
    """Smallest request count at which ONE error is still inside the budget's resolution.

    Below it the gate is not measuring the budget, it is measuring rounding: on a 5-request window a
    1% budget can only ever read as 0% or 20%, so a `pass` means "no errors happened to land in five
    requests", which is not evidence about a release.
    """
    if max_error_rate <= 0:
        return 0
    return int(-(-1 // max_error_rate))  # ceil(1 / max_error_rate)


def evaluate_slo_window(error_before: float | None, error_after: float | None,
                        request_before: float | None, request_after: float | None,
                        probe_requests: int, max_error_rate: float = 0.01, *,
                        continuity: tuple[str, str] | None = None,
                        unrated_errors: float | None = None) -> tuple[str, str]:
    """pass | fail | blocked — the error budget over the probe window, not over counter history.

    `probe_requests` is the number of in-scope requests the gate itself delivered to the candidate
    inside the window. It is the ATTRIBUTION FLOOR: the scraped process must have observed at least
    that many, or the burst and the scrape did not meet.

    `unrated_errors` is the count of errors in the window that the denominator provably CANNOT count,
    reported so that narrowing the numerator to the denominator's population is never silent. Honua
    records catalog-level GeoServices errors (`service_type="GeoServices"`, e.g. a 401 from an
    unauthenticated scanner hitting a protected `/rest/services` route) against a surface that emits
    no `honua_serving_request_duration_ms_count` series at all — verified live: the demo exposes
    honua_protocol values FeatureServer and MapServer only, and its catalog requests appear in no
    denominator series. Rating those errors against a denominator that excludes their requests is not
    a strict reading of the budget, it is an arithmetic error whose size depends on how many strangers
    port-scanned the demo during the window — precisely the traffic-timing dependence this window
    exists to remove. They are therefore counted, named, and NOT rated. Every error on a surface the
    denominator does count — including the HTTP-200-with-{error} in-band class this gate exists for —
    is still in the numerator.
    """
    status, why = (continuity or ("pass", "process continuity not checked"))
    if status != "pass":
        return "blocked", f"window measurement is not a rate — {why}"
    continuity_why = why

    requests, invalid = counter_delta(request_before, request_after)
    if invalid is not None:
        return "blocked", f"request denominator unusable — {invalid}"
    errors, invalid = counter_delta(error_before, error_after)
    if invalid is not None:
        return "blocked", f"error numerator unusable — {invalid}"

    if probe_requests <= 0:
        return "blocked", ("no probe traffic was delivered to the candidate, so the window has no "
                           "denominator this gate can vouch for")
    if requests < probe_requests:
        return "blocked", (f"probe attribution incomplete: the scraped instance observed "
                           f"{requests:g} in-scope requests but the gate delivered {probe_requests} — "
                           "the burst was spread across instances, or the scrape landed on one that "
                           "did not serve it, so the delta is a fraction of an unknown population")

    floor = minimum_resolvable_window(max_error_rate)
    if requests < floor:
        return "blocked", (f"window too small to resolve a {max_error_rate:.4f} budget: {requests:g} "
                           f"requests, where a single error reads as {1 / requests:.4f}; need at least "
                           f"{floor}")

    rate = errors / requests
    verdict = "fail" if rate > max_error_rate else "pass"
    detail = (f"error rate {rate:.4f} {'exceeds' if verdict == 'fail' else 'within'} budget "
              f"{max_error_rate:.4f} over a bounded probe window ({int(errors)}/{int(requests)} "
              f"in-scope requests, {probe_requests} of them driven by this gate) [{continuity_why}]")
    if unrated_errors:
        detail += (f" [plus {int(unrated_errors)} error(s) on GeoServices surfaces the denominator "
                   "emits no request series for — reported, not rated]")
    return verdict, detail


def service_root_url(metrics_url: str | None) -> str | None:
    """Derive the candidate's GeoServices catalog URL from its /metrics URL.

    Same-origin by construction, for the same reason capability_manifest_url is: the instance the
    probe drives traffic at MUST be the instance whose counters are being differenced, and that is
    only guaranteed if neither URL can be configured independently of the other.
    """
    parts = urllib.parse.urlsplit((metrics_url or "").strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/rest/services", "", ""))


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


def evaluate_gate(error_before: float | None, error_after: float | None,
                  request_before: float | None, request_after: float | None,
                  probe_requests: int, max_error_rate: float = 0.01, *,
                  continuity: tuple[str, str] | None = None,
                  unrated_errors: float | None = None,
                  instance_revision: str | None = None,
                  pinned_sha: str | None = None,
                  revision_source: str | None = None) -> tuple[str, str]:
    """The gate verdict: candidate identity first, windowed error budget second.

    Identity is a precondition, not a co-equal signal, so it short-circuits. That ordering is what
    makes `pass` unreachable for an instance whose identity was not confirmed — the budget is never
    even consulted.
    """
    identity_status, identity_why = evaluate_candidate_identity(instance_revision, pinned_sha, revision_source)
    if identity_status != "pass":
        return "blocked", f"candidate identity unconfirmed — {identity_why}"
    status, why = evaluate_slo_window(error_before, error_after, request_before, request_after,
                                      probe_requests, max_error_rate, continuity=continuity,
                                      unrated_errors=unrated_errors)
    return status, f"{why} [candidate identity: {identity_why}]"


def _opt_float(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _opt_int(v: str | None) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0


def parse_continuity(value: str | None) -> tuple[str, str]:
    """CLI encoding of the continuity check: "ok[: why]" passes, anything else blocks.

    Empty means the probe never reported, which is a BLOCK rather than a default-pass: the whole
    point of the witness is that an unproven window is not a rate.
    """
    text = (value or "").strip()
    if not text:
        return "blocked", "the probe reported no process-continuity witness for the window"
    head, _, tail = text.partition(":")
    if head.strip().lower() == "ok":
        return "pass", tail.strip() or "process continuity confirmed by the probe"
    return "blocked", text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    # The window: two scrapes of each cumulative series, bracketing a bounded probe burst. Empty
    # means the series was ABSENT from that scrape, which counter_delta distinguishes from zero.
    ap.add_argument("--error-before", default=None, help="honua_geoservices_error_total before the window")
    ap.add_argument("--error-after", default=None, help="honua_geoservices_error_total after the window")
    ap.add_argument("--request-before", default=None, help="scoped request counter before the window")
    ap.add_argument("--request-after", default=None, help="scoped request counter after the window")
    ap.add_argument("--probe-requests", default="0",
                    help="in-scope requests this gate delivered inside the window (attribution floor)")
    ap.add_argument("--continuity", default="",
                    help="'ok[: detail]' when the probe proved one process answered both scrapes; "
                         "otherwise the reason it could not, which blocks")
    ap.add_argument("--unrated-errors", default="0",
                    help="errors in the window on GeoServices surfaces the denominator emits no "
                         "request series for — reported in the verdict, never rated")
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

    status, why = evaluate_gate(_opt_float(args.error_before), _opt_float(args.error_after),
                                _opt_float(args.request_before), _opt_float(args.request_after),
                                _opt_int(args.probe_requests), args.max_error_rate,
                                continuity=parse_continuity(args.continuity),
                                unrated_errors=_opt_float(args.unrated_errors),
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
