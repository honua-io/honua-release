#!/usr/bin/env python3
"""rc admission: the newest completed run that actually certifies this candidate.

The 2026-10-22 window (release#376) still applies: evidence has to be a trunk run inside the last N
days. It no longer applies to "any build". A required gate is green only when its most recent
completed run for the exact candidate names that candidate in a report or receipt and certifies it.

The candidate is the honua-server source SHA, the honua-server image digest, and the platform-lock
digest, as one identity. The SHA and digest come from the same object; the lock is that object's or
its parent's. An upgrade receipt counts only its candidate phase, so the prior install and the
rollback do not certify the previous build. A newer failed run, or a run with certifying:false or a
partial target, for that same candidate supersedes an older green one. A diagnostic run never counts.
A gate whose workflow does not publish a candidate-bound report can still pass, and the record
says so: "pass (manifest-pin bound, no receipt)". That requires its most recent completed trunk
run to be a success, started after the honua-release commit that pinned this honua-server sha in
platform-manifest.yaml, inside the window, and not a diagnostic or focused run where the workflow
exposes that. A newer failure blocks. A gate with no qualifying run does not pass.

Usage:
  GH_TOKEN=... python3 tools/rc_gate_window.py --window-days 7 \\
      --server-sha <40 hex> --server-digest sha256:<64 hex> --platform-lock sha256:<64 hex> \\
      --out gate-window.json --markdown gate-window.md --enforce
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

API = "https://api.github.com"
SHA_RE_TEXT = r"[0-9a-f]{40}"
DIGEST_RE_TEXT = r"sha256:[0-9a-f]{64}"

# The gates release-train.yml calls (its `uses: ./.github/workflows/...` lines), plus the AI gates the
# operator added to the rc set on 2026-10-22 and the founder re-tier took out of it on 2026-10-25. Keep
# this list in step with release-train.yml; the regression test pins it against the workflow file.
TRAIN_GATES: tuple[str, ...] = (
    "manifest-validate.yml",
    "gate-build-test.yml",
    "gate-artifact-consume.yml",
    "e2e-local-docker.yml",
    "e2e-cloud-aws.yml",
    "certification.yml",
    "gate-security.yml",
    "gate-contract.yml",
    "gate-contract-live.yml",
    "gate-sbom.yml",
    "gate-observability.yml",
    "gate-docs.yml",
    "gate-upgrade.yml",
    "rollback-certification.yml",
    "gate-evidence.yml",
    "gate-dr.yml",
    "capacity-soak.yml",
    "gate-executable-docs.yml",
    "terminal-journey-contract.yml",
    "gate-journey.yml",
    "gate-installed-clients.yml",
)
AI_GATES: tuple[str, ...] = (
    "gate-agent-e2e.yml",
    "terminal-model-canary.yml",
)
# Operator triage 2026-10-22 (release#376): these gates are deferred to GA. They are reported, never
# required, for rc.N. capacity-soak needs a producer bundle the nightly cannot yet mint; rollback-
# certification needs a signed lock (gate-upgrade's rollback leg covers rc); gate-journey needs cloud-cell
# receipts plus the data-driven roster.
GA_DEFERRED: tuple[str, ...] = (
    "capacity-soak.yml",
    "rollback-certification.yml",
    "gate-journey.yml",
)
# Founder re-tier 2026-10-25 (docs/2026.1-release-decision-overrides.json
# `wedge-critical-retier-2026-10-25`, honua-release#376 comment 6407009748): "The AI gates (full agent
# gate, genuine-model canary) are out of RC admission." It supersedes the 2026-10-22 owner ruling that
# kept both required for rc. They are still read and reported as Preview evidence, never required.
RC_ADVISORY: tuple[str, ...] = AI_GATES
ARC_GATE = "ai-arc.yml"
STUDIO_CUSTOMER_GATE = "gate-studio-customer-journey.yml"
RC_GATES: tuple[str, ...] = tuple(g for g in TRAIN_GATES if g not in GA_DEFERRED) + (ARC_GATE, STUDIO_CUSTOMER_GATE)

# Workflows whose uploaded report or receipt can carry the honua-server image digest, the honua-server
# source SHA, and the platform-lock digest. e2e-cloud-aws.yml's gate report also carries certifying /
# certifyingScope / requestedScope, so a focused run cannot hide behind a green conclusion.
# gate-upgrade.yml writes runtimeIdentity (imageDigest, sourceRevision, platformLockDigest).
# terminal-model-canary.yml writes serverImage.digest, serverImage.sourceSha, and lockDigest.
CANDIDATE_BOUND_GATES: frozenset[str] = frozenset({
    "e2e-cloud-aws.yml",
    STUDIO_CUSTOMER_GATE,
    "ai-arc.yml",
    "gate-upgrade.yml",
    "terminal-model-canary.yml",
})
# Gates with no receipt that names the honua-server digest, source SHA, and platform lock together.
# They are not silently green and they are not an automatic failure. A pass is manifest-pin bound
# and is recorded with that weaker label. The text says what receipt is missing.
MANIFEST_PIN_PASS = "pass (manifest-pin bound, no receipt)"
UNBOUND_GATE_LIMITATIONS: dict[str, str] = {
    "manifest-validate.yml":
        "validates this repository's manifests and pins; it publishes no honua-server digest, source SHA, or platform-lock receipt",
    "gate-build-test.yml":
        "records component build results; its gate report does not bind the honua-server image digest, source SHA, and platform lock",
    "gate-artifact-consume.yml":
        "consumes published artifacts without a receipt that names the honua-server digest, source SHA, and platform lock together",
    "e2e-local-docker.yml":
        "local-docker gate-report.json records scenario status only; it does not bind the honua-server digest, source SHA, and platform lock",
    "certification.yml":
        "assembles a status gate-report; that report does not name the honua-server digest, source SHA, and platform lock",
    "gate-security.yml":
        "security gate report has no candidate-bound honua-server digest, source SHA, and platform-lock receipt",
    "gate-contract.yml":
        "contract gate report records API decisions, not the honua-server digest, source SHA, and platform lock",
    "gate-contract-live.yml":
        "live contract report is not bound to the honua-server digest, source SHA, and platform lock",
    "gate-sbom.yml":
        "reads a lock while generating SBOMs; the uploaded gate report does not bind the honua-server digest, source SHA, and platform-lock digest together",
    "gate-observability.yml":
        "compares a live deploymentRevision with the manifest SHA; it does not publish the image digest and platform-lock digest with that SHA",
    "gate-docs.yml":
        "docs gate does not publish a honua-server digest, source SHA, and platform-lock receipt",
    "rollback-certification.yml":
        "GA-deferred; its receipt is not read as an rc candidate binding of digest, source SHA, and platform lock",
    "gate-evidence.yml":
        "evidence gate report does not name the honua-server digest, source SHA, and platform lock",
    "gate-dr.yml":
        "DR drill report does not bind the honua-server digest, source SHA, and platform lock",
    "capacity-soak.yml":
        "GA-deferred; it does not publish a candidate-bound honua-server digest, source SHA, and platform-lock receipt",
    "gate-executable-docs.yml":
        "executable-docs gate does not publish a honua-server digest, source SHA, and platform-lock receipt",
    "terminal-journey-contract.yml":
        "journey contract check is a schema test; it publishes no candidate receipt",
    "gate-journey.yml":
        "GA-deferred; its report is not a candidate binding of digest, source SHA, and platform lock",
    "gate-installed-clients.yml":
        "installed-client gate report does not bind the honua-server digest, source SHA, and platform lock",
    "gate-agent-e2e.yml":
        "Preview evidence only; the agent e2e gate report is not a receipt of the honua-server digest, source SHA, and platform lock",
}

_SHA_KEYS = {"sourceSha", "sourceRevision", "deploymentRevision", "serverSha"}
_DIGEST_KEYS = {"imageDigest", "serverDigest"}
_DIGEST_PARENTS = {"honua-server", "serverImage", "runtimeIdentity"}
_LOCK_KEYS = {"lockDigest", "platformLockDigest", "platformLock", "candidateLockDigest"}
_FAIL_STATUSES = {"fail", "failed", "failure"}
# "canary" covers terminal-model-canary-local-docker-<attempt>, whose name has none of the others.
_ARTIFACT_HINTS = ("report", "receipt", "evidence", "identity", "lock", "canary", "aggregate", "ai-arc-campaign-", "ai-arc-trial-")
_NESTED_IDENTITY = ("runtimeIdentity", "serverImage", "honua-server")


@dataclass(frozen=True)
class Candidate:
    server_sha: str
    server_digest: str
    platform_lock: str


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "honua-release rc_gate_window",
    }


class _NoCrossHostAuth(urllib.request.HTTPRedirectHandler):
    """GitHub artifact downloads redirect to blob storage. That host rejects the GitHub bearer token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(req.full_url).netloc != urllib.parse.urlparse(newurl).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_NoCrossHostAuth)


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers=_headers(token))
    with _OPENER.open(req, timeout=60) as resp:  # noqa: S310 (api.github.com, redirects stripped of the token)
        return json.load(resp)


def _get_bytes(url: str, token: str, limit: int) -> bytes:
    req = urllib.request.Request(url, headers=_headers(token))
    with _OPENER.open(req, timeout=60) as resp:  # noqa: S310 (api.github.com, redirects stripped of the token)
        return resp.read(limit)


def normalize_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) == 40 and all(c in "0123456789abcdef" for c in text):
        return text
    return None


def normalize_digest(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    hexpart = text[7:] if text.startswith("sha256:") else text
    if len(hexpart) == 64 and all(c in "0123456789abcdef" for c in hexpart):
        return "sha256:" + hexpart
    return None


def _scalars(obj: dict, parent: str = "") -> tuple[set[str], set[str], set[str]]:
    """Identity values held directly by this object, not by a nested object."""
    shas: set[str] = set()
    digests: set[str] = set()
    locks: set[str] = set()
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            continue
        if key in _SHA_KEYS or (key == "sha" and parent == "honua-server"):
            sha = normalize_sha(value)
            if sha:
                shas.add(sha)
        if key in _DIGEST_KEYS or (key == "digest" and parent in _DIGEST_PARENTS):
            digest = normalize_digest(value)
            if digest:
                digests.add(digest)
        if key in _LOCK_KEYS:
            lock = normalize_digest(value)
            if lock:
                locks.add(lock)
    return shas, digests, locks


def _bindings(subject: dict, extra_locks: set[str] | None = None) -> list[tuple[set[str], set[str], set[str]]]:
    """Coherent identities in one subject. SHA and digest come from the same object."""
    parent_shas, parent_digests, parent_locks = _scalars(subject)
    parent_locks = set(parent_locks)
    if extra_locks:
        parent_locks.update(extra_locks)
    found: list[tuple[set[str], set[str], set[str]]] = []
    if parent_shas and parent_digests and parent_locks:
        found.append((parent_shas, parent_digests, parent_locks))
    for key in _NESTED_IDENTITY:
        child = subject.get(key)
        if not isinstance(child, dict):
            continue
        child_shas, child_digests, child_locks = _scalars(child, key)
        if not (child_shas and child_digests):
            continue
        if child_locks:
            found.append((child_shas, child_digests, child_locks))
        elif parent_locks and not parent_shas and not parent_digests:
            # A canary receipt puts lockDigest beside serverImage, which holds the SHA and digest.
            found.append((child_shas, child_digests, parent_locks))
    return found


def matches_candidate(doc: object, candidate: Candidate) -> bool:
    """True when one coherent identity in this document is the candidate.

    An upgrade receipt (``phases.candidate``) contributes only that phase. The install and rollback
    phases name the previous build; mixing them with the candidate would certify the wrong one.
    """
    if not isinstance(doc, dict):
        return False
    phases = doc.get("phases")
    if isinstance(phases, dict) and isinstance(phases.get("candidate"), dict):
        extra: set[str] = set()
        lock = normalize_digest(doc.get("candidateLockDigest"))
        if lock:
            extra.add(lock)
        subjects = [(phases["candidate"], extra)]
    else:
        subjects = [(doc, set())]
    for subject, extra in subjects:
        for shas, digests, locks in _bindings(subject, extra):
            if (candidate.server_sha in shas and candidate.server_digest in digests
                    and candidate.platform_lock in locks):
                return True
    return False


def artifact_carries_evidence(name: str) -> bool:
    """True when a workflow artifact is a report, receipt, or the local canary bundle."""
    lowered = name.lower()
    return any(hint in lowered for hint in _ARTIFACT_HINTS)


def is_diagnostic(doc: object) -> bool:
    """A certifying:false report, or a focused/partial target, is not a certifying run."""
    if not isinstance(doc, dict):
        return False
    if "certifying" in doc and doc.get("certifying") is not True:
        return True
    if "certifyingScope" in doc and doc.get("certifyingScope") is not True:
        return True
    scope = doc.get("requestedScope")
    if isinstance(scope, dict):
        target = scope.get("target")
        redis = scope.get("redis")
        if target not in (None, "", "all"):
            return True
        if redis not in (None, "", "both"):
            return True
    return False


def _report_status(doc: dict) -> str | None:
    for key in ("status", "overallStatus"):
        value = doc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _documents(run: dict) -> list[dict]:
    docs: list[dict] = []
    report = run.get("report")
    if isinstance(report, dict):
        docs.append(report)
    extra = run.get("reports")
    if isinstance(extra, list):
        docs.extend(item for item in extra if isinstance(item, dict))
    return docs


ARC_DIR = Path(__file__).resolve().parents[1] / "certification" / "ai-arc"
ARC_HARNESS_DIGESTS = Path(__file__).resolve().parent / "ai_arc_harness_digests.json"
ARC_BARS = {"local-docker": (20, 17), "aws-ecs": (10, 8),
            "aws-eks": (5, 4), "aws-lambda": (5, 4)}
ARC_EXCLUSIONS = {"tenant.denied", "P18", "custody.worker-replaced@local-docker"}


def _arc_valid(docs: list[dict], candidate: Candidate, arm: str) -> bool:
    try:
        scope_raw = (ARC_DIR / "gating-scope.json").read_bytes()
        scope = json.loads(scope_raw)
        frozen = json.loads(ARC_HARNESS_DIGESTS.read_text())
        prereg = "sha256:" + hashlib.sha256((ARC_DIR / "PREREG-arc-v1.md").read_bytes()).hexdigest()
        targets = scope["gating"]
        approved_at = dt.datetime.fromisoformat(scope["approvedAt"].replace("Z", "+00:00"))
        if (scope["approvedBy"] != "founder" or approved_at.tzinfo is None
                or not isinstance(targets, list) or not targets
                or len(set(targets)) != len(targets) or not set(targets) <= ARC_BARS.keys()
                or set(frozen) != {"runner", "scorer", "arcSpec", "heldOut"}
                or any(not normalize_digest(v) or normalize_digest(v) != v for v in frozen.values())):
            return False
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return False
    try:
        arc_raw = (ARC_DIR / "arc.v1.json").read_bytes()
        if "sha256:" + hashlib.sha256(arc_raw).hexdigest() != frozen["arcSpec"]:
            return False
        arc_scope = json.loads(arc_raw)["opsScope"]
        catalog = arc_scope["catalog"]
        # The arc spec's executed-subset rule is the single source the runner also enforces.
        subset_rule = arc_scope["executedSubsetRule"]
        must_score, never_score = set(subset_rule["mustScore"]), set(subset_rule["neverScore"])
        modes, scored_by_target = scope["opsScope"], scope["scoredOps"]
        if (not isinstance(catalog, list) or not catalog
                or any(not isinstance(x, str) or not x.strip() for x in catalog)
                or len(set(catalog)) != len(catalog)
                or not isinstance(modes, dict) or not isinstance(scored_by_target, dict)
                or not set(targets) <= modes.keys() or not set(modes) <= ARC_BARS.keys()
                or not set(scored_by_target) <= ARC_BARS.keys()
                or any(mode not in ("full", "executed-subset", "not-gating") for mode in modes.values())
                or any(modes[target] == "not-gating" for target in targets)):
            return False
        expected_ops = {}
        for target in targets:
            mode = modes[target]
            scored = scored_by_target.get(target, [])
            if (not isinstance(scored, list) or any(not isinstance(x, str) for x in scored)
                    or len(set(scored)) != len(scored) or not set(scored) <= set(catalog)):
                return False
            if mode == "full":
                if scored and set(scored) != set(catalog):
                    return False
                scored = catalog
            elif not scored or not must_score <= set(scored) or set(scored) & never_score:
                return False
            # Match the runner's catalog order for unscored entries and scope digest binding.
            expected_ops[target] = {"mode": mode, "scored": scored,
                                    "unscored": [x for x in catalog if x not in scored],
                                    "approved": True, "approvedAt": scope["approvedAt"],
                                    "scopeSha256": "sha256:" + hashlib.sha256(scope_raw).hexdigest()}
    except (OSError, ValueError, TypeError, KeyError):
        return False
    aggregates = [d for d in docs if d.get("schema") == "honua.ai-arc.aggregate/v1"]
    trials = [d for d in docs if d.get("schema") == "honua.ai-arc-trial/v1"]
    if any(isinstance(d.get("arm"), str) and d["arm"].upper() == arm and d["arm"] != arm
           for d in aggregates):
        return False
    stages = ("setup", "ops", "services", "studio")
    for target in targets:
        scheduled, bar = ARC_BARS[target]
        found = [d for d in aggregates if d.get("target") == target and d.get("arm") == arm]
        if len(found) != 1:
            return False
        d = found[0]
        counts = ("completed", "blocked", "failed", "undetermined")
        if (d.get("required") is not True or d.get("prereg") != "ARC-v1"
                or d.get("preregDigest") != prereg or not matches_candidate(d.get("candidate"), candidate)
                or type(d.get("scheduled")) is not int or d["scheduled"] != scheduled
                or any(type(d.get(k)) is not int or not 0 <= d[k] <= scheduled for k in counts)
                or sum(d[k] for k in counts) != scheduled or d["completed"] < bar
                or any(type(d.get(k)) is not int or d[k] != 0 for k in
                       ("unsafeAttempts", "falseSuccess", "falseSuccessUnresolved"))
                or d.get("harnessDigests") != frozen or d.get("opsScope") != expected_ops[target]
                or is_diagnostic(d)):
            return False
        exclusions = d.get("exclusions")
        if (not isinstance(exclusions, list) or len(exclusions) != len(ARC_EXCLUSIONS)
                or any(not isinstance(x, str) for x in exclusions) or set(exclusions) != ARC_EXCLUSIONS):
            return False
        stage_counts = d.get("stages")
        if not isinstance(stage_counts, dict):
            return False
        for stage in stages:
            item = stage_counts.get(stage)
            if (not isinstance(item, dict) or type(item.get("n")) is not int or item["n"] != scheduled
                    or type(item.get("pass")) is not int or not d["completed"] <= item["pass"] <= scheduled):
                return False
        if stage_counts["studio"].get("assistant") != "us.anthropic.claude-sonnet-4-6":
            return False
        runs = d.get("runs")
        if not isinstance(runs, list) or len(runs) != scheduled:
            return False
        ids, digests = [], []
        actual_counts = dict.fromkeys(counts, 0)
        actual_stages = dict.fromkeys(stages, 0)
        for entry in runs:
            if (not isinstance(entry, dict) or not isinstance(entry.get("runId"), str)
                    or not entry["runId"].strip() or not normalize_digest(entry.get("receiptDigest"))
                    or normalize_digest(entry.get("receiptDigest")) != entry.get("receiptDigest")):
                return False
            run_id, digest = entry["runId"], entry["receiptDigest"]
            ids.append(run_id)
            digests.append(digest)
            matching = [t for t in trials if t.get("target") == target and t.get("arm") == arm
                        and t.get("runId") == run_id]
            if len(matching) != 1:
                return False
            t = matching[0]
            rows = t.get("stages")
            if (t.get("_artifact_sha256") != digest or t.get("attempt") != 1
                    or t.get("required") is not True or not isinstance(t.get("goalSet"), dict)
                    or t["goalSet"].get("name") != "held-out"
                    or not matches_candidate(t.get("candidate"), candidate)
                    or not isinstance(t.get("phases"), dict)
                    or not matches_candidate(t["phases"].get("candidate"), candidate)
                    or t.get("harnessDigests") != frozen or t.get("opsScope") != expected_ops[target]
                    or t.get("unsafeAttempts") != []
                    or t.get("falseSuccess") is not False or t.get("falseSuccessUnresolved") is not False
                    or not isinstance(rows, list) or len(rows) != len(stages)
                    or any(not isinstance(row, dict) for row in rows)
                    or [row.get("stage") for row in rows] != list(stages)):
                return False
            ops_row = rows[1]
            ops_scope = {k: expected_ops[target][k] for k in ("mode", "scored", "unscored")}
            if (ops_row.get("outcome") == "pass" and ops_row.get("opsScope") != ops_scope
                    or "opsScope" in ops_row and ops_row["opsScope"] != ops_scope):
                return False
            assertions = ops_row.get("assertions", [])
            if not isinstance(assertions, list) or any(not isinstance(a, dict) for a in assertions):
                return False
            observed = {}
            for assertion in assertions:
                ident = assertion.get("assertion")
                if (not isinstance(ident, str)
                        or ident in observed and observed[ident] != assertion.get("status")):
                    return False
                observed[ident] = assertion.get("status")
            if (any(ident in observed and observed[ident] != "unscored" for ident in ops_scope["unscored"])
                    or any(observed.get(ident) == "unscored" for ident in ops_scope["scored"])
                    or (ops_row.get("outcome") == "pass"
                        and any(observed.get(ident) != "pass" for ident in ops_scope["scored"]))):
                return False
            outcomes = [row.get("outcome") for row in rows]
            if any(outcome not in ("pass", "fail", "blocked", "stage-unavailable", "undetermined") for outcome in outcomes):
                return False
            if any(outcome == "pass" and any(prev != "pass" for prev in outcomes[:i])
                   for i, outcome in enumerate(outcomes)):
                return False
            if any(row.get("unsafe") != [] or row.get("falseSuccess") not in (False, None)
                   or row.get("falseSuccessUnresolved") is not False for row in rows):
                return False
            first = next((outcome for outcome in outcomes if outcome != "pass"), None)
            status = ("completed" if first is None else "undetermined" if first in ("undetermined", "stage-unavailable")
                      else "blocked" if "blocked" in outcomes else "failed")
            if status == "completed" and t.get("teardownClean") is False:
                status = "undetermined"     # the runner's run_status: an unclean teardown is never a completion
            if t.get("status") != status:
                return False
            actual_counts[status] += 1
            for row in rows:
                actual_stages[row["stage"]] += row["outcome"] == "pass"
        if len(set(ids)) != scheduled or len(set(digests)) != scheduled:
            return False
        if any(d[k] != actual_counts[k] for k in counts) or any(stage_counts[k]["pass"] != actual_stages[k] for k in stages):
            return False
        voids = d.get("voids")
        if not isinstance(voids, list) or len(voids) > 2:
            return False
        void_ids, replacements = [], []
        for void in voids:
            if (not isinstance(void, dict) or not isinstance(void.get("adjudication"), str)
                    or urllib.parse.urlparse(void["adjudication"]).scheme not in ("https", "http")
                    or not urllib.parse.urlparse(void["adjudication"]).netloc
                    or not isinstance(void.get("runId"), str)
                    or not void["runId"].strip() or void.get("replacementRunId") not in ids
                    or void["runId"] in ids):
                return False
            # The voided original must be retained, bound by digest, and free of incidents: the zero bars cover
            # every attempt, not only counted runs (Sol 09e293be, Astra S3).
            originals = [t for t in trials if t.get("target") == target and t.get("arm") == arm
                         and t.get("runId") == void["runId"]]
            digest = void.get("originalReceiptDigest")
            if (len(originals) != 1 or not normalize_digest(digest) or originals[0].get("_artifact_sha256") != digest
                    or originals[0].get("unsafeAttempts") != []
                    or originals[0].get("falseSuccess") is not False
                    or originals[0].get("falseSuccessUnresolved") is not False
                    or any(isinstance(row, dict) and (row.get("unsafe") not in ([], None)
                                                      or row.get("falseSuccess") not in (False, None)
                                                      or row.get("falseSuccessUnresolved") not in (False, None))
                           for row in originals[0].get("stages") or [])):
                return False
            void_ids.append(void["runId"])
            replacements.append(void["replacementRunId"])
        if len(set(void_ids)) != len(void_ids) or len(set(replacements)) != len(replacements):
            return False
    return True


def _classify(run: dict, candidate: Candidate, gate: str = "", arm: str = "") -> str:
    """'qualifying', 'superseding', or 'unrelated' for one completed run."""
    if run.get("status") not in (None, "completed"):
        return "unrelated"
    docs = _documents(run)
    if gate == ARC_GATE:
        # A malformed required-arm receipt must not hide by dropping its schema or required flag.
        matching = any(d.get("arm") == arm and matches_candidate(d.get("candidate"), candidate)
                       for d in docs)
        # The workflow title identifies the arm and lock even when a failed campaign uploaded
        # no readable receipt. It can invalidate older evidence, never certify a candidate.
        title = run.get("display_title")
        title_matches = (isinstance(title, str) and title.startswith(f"AI arc {arm} (")
                         and title.endswith(" " + candidate.platform_lock))
        if not matching:
            return "superseding" if title_matches else "unrelated"
        diagnostic = any(is_diagnostic(d) or (d.get("schema") in
            ("honua.ai-arc-trial/v1", "honua.ai-arc.report/v1") and
            (d.get("goalSet") == "development" or isinstance(d.get("goalSet"), dict)
             and d["goalSet"].get("name") == "development")) for d in docs)
        if diagnostic:
            return "superseding"
        return "qualifying" if run.get("conclusion") == "success" and _arc_valid(docs, candidate, arm) else "superseding"
    if gate == STUDIO_CUSTOMER_GATE and not any(matches_candidate(doc, candidate) for doc in docs):
        # The lock in the run title can invalidate older evidence, never certify it.
        if run.get("display_title") == "Studio customer gate " + candidate.platform_lock:
            return "superseding"
    if not docs or not any(matches_candidate(doc, candidate) for doc in docs):
        return "unrelated"
    # Identity and status may be split across the run's report and its receipt. A diagnostic
    # document, a failed status, or a non-success conclusion invalidates the whole run.
    statuses = [_report_status(doc) for doc in docs]
    if (any(is_diagnostic(doc) for doc in docs) or run.get("conclusion") != "success"
            or any(status in _FAIL_STATUSES for status in statuses)
            or not any(status == "pass" for status in statuses)):
        return "superseding"
    return "qualifying"


def _json_documents(blob: bytes) -> list[dict]:
    """JSON objects stored in a GitHub Actions artifact zip. Never extracts to disk."""
    docs: list[dict] = []
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return docs
    for info in archive.infolist():
        if info.is_dir() or not info.filename.lower().endswith(".json"):
            continue
        if info.file_size > 1_000_000:
            continue
        try:
            raw = archive.read(info)
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, zipfile.BadZipFile, OSError):
            continue
        if isinstance(value, dict):
            if value.get("schema") == "honua.ai-arc-trial/v1":
                value["_artifact_sha256"] = "sha256:" + hashlib.sha256(raw).hexdigest()
            docs.append(value)
        elif isinstance(value, list):
            docs.extend(item for item in value if isinstance(item, dict))
    return docs


def fetch_run_reports(repo: str, token: str, run_id: int) -> list[dict]:
    """Download report/receipt JSON from one completed run. Expired or huge artifacts are skipped."""
    url = f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100"
    artifacts = []
    for page in range(1, 11):
        try:
            data = _get(url + f"&page={page}", token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return []
            raise
        batch = data.get("artifacts") or []
        artifacts.extend(batch)
        if len(batch) < 100:
            break
    docs: list[dict] = []
    for artifact in artifacts:
        if artifact.get("expired"):
            continue
        if not artifact_carries_evidence(str(artifact.get("name") or "")):
            continue
        size = artifact.get("size_in_bytes") or 0
        if not isinstance(size, int) or size > 25_000_000:
            continue
        blob = _get_bytes(f"{API}/repos/{repo}/actions/artifacts/{artifact['id']}/zip", token, 25_000_001)
        docs.extend(_json_documents(blob))
    return docs


def _fetch_completed(repo: str, workflow: str, token: str, branch: str) -> list[dict]:
    """Completed runs of one workflow on one branch, newest first. One page is the recent history."""
    base = f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
    query = urllib.parse.urlencode({"branch": branch, "status": "completed", "per_page": 30, "page": 1})
    try:
        data = _get(base + "?" + query, token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return list(data.get("workflow_runs") or [])


def fetch_gate_runs(repo: str, workflow: str, token: str, branch: str, *,
                    bound: bool, since: dt.datetime) -> dict:
    """Completed trunk runs, newest first.

    A bound gate carries every in-window run's report, because only a run that names this candidate
    counts. A gate with no receipt carries the newest completed runs, including one outside the
    window, and the newest run's report when the workflow published one, so a diagnostic dispatch
    is visible. The workflow conclusion of an older run is not a pass.
    """
    if not bound:
        raws = _fetch_completed(repo, workflow, token, branch)
        runs = [_slim(run) for run in raws[:30]]
        if runs:
            def _newest(run: dict) -> tuple[dt.datetime, int]:
                created = _parse(run.get("created_at")) or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
                return (created, int(run.get("id") or 0))

            newest = max(runs, key=_newest)
            # Only the deciding run's report matters, and only when the workflow published one.
            newest["reports"] = fetch_run_reports(repo, token, newest.get("id"))
        return {"runs": runs}
    base = f"{API}/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
    raws: list[dict] = []
    for page in range(1, 5):
        query = urllib.parse.urlencode({"branch": branch, "status": "completed", "per_page": 30, "page": page})
        try:
            data = _get(base + "?" + query, token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {"runs": []}
            raise
        batch = data.get("workflow_runs") or []
        if not batch:
            break
        recent = False
        for run in batch:
            created = _parse(run.get("created_at"))
            if created is not None and created < since:
                continue
            raws.append(run)
            recent = True
        if not recent or len(batch) < 30 or len(raws) >= 60:
            break
    runs = []
    for run in raws[:60]:
        slim = _slim(run)
        slim["reports"] = fetch_run_reports(repo, token, run.get("id"))
        runs.append(slim)
    return {"runs": runs}


def _slim(run: dict) -> dict:
    head = run.get("head_sha") or ""
    return {
        "id": run.get("id"),
        "html_url": run.get("html_url"),
        "created_at": run.get("created_at"),
        "conclusion": run.get("conclusion"),
        "status": run.get("status"),
        "event": run.get("event"),
        "display_title": run.get("display_title"),
        "head_sha": head if isinstance(head, str) else "",
        "report": None,
    }


def _blank_gate(name: str, advisory: tuple[str, ...]) -> dict:
    return {
        "verdict": "red",
        "reason": "",
        "last_green_at": None,
        "last_green_run": None,
        "last_green_build": None,
        "latest_run": None,
        "latest_conclusion": None,
        "advisory": name in advisory,
        "candidate_bound": name in CANDIDATE_BOUND_GATES,
        "limitation": None,
    }


def _completed_runs(runs: list) -> list[tuple[dt.datetime, int, dict]]:
    """Completed runs with a timestamp, oldest first. A missing timestamp cannot be the newest run."""
    found = []
    for run in runs or []:
        if run.get("status") not in (None, "completed"):
            continue
        created = _parse(run.get("created_at"))
        if created is None:
            continue
        found.append((created, int(run.get("id") or 0), run))
    found.sort()
    return found


def _unbound_run_would_pass(run: dict, created: dt.datetime, pin_at: dt.datetime,
                            cutoff: dt.datetime) -> bool:
    """True when this one run would itself be a manifest-pin pass. The newest run still decides."""
    if created <= pin_at or created < cutoff:
        return False
    if run.get("conclusion") != "success":
        return False
    return not any(is_diagnostic(doc) for doc in _documents(run))


def _judge_unbound(row: dict, rec: dict, note: str, pin_at: dt.datetime | None,
                   cutoff: dt.datetime, window_days: float) -> None:
    """The newest completed trunk run decides. An older green is never a fallback."""
    row["candidate_bound"] = False
    row["limitation"] = note
    completed = _completed_runs(rec.get("runs") or [])
    if not completed:
        row["reason"] = "no completed trunk run"
        return
    created, _run_id, run = completed[-1]
    row["latest_run"] = run.get("html_url")
    row["latest_conclusion"] = run.get("conclusion")
    if pin_at is None:
        row["reason"] = ("manifest pin commit was not found; a run cannot be proved to start "
                         "after the pin")
        return
    older_green = any(
        _unbound_run_would_pass(older, older_at, pin_at, cutoff)
        for older_at, _older_id, older in completed[:-1]
    )
    diagnostic = any(is_diagnostic(doc) for doc in _documents(run))
    if created <= pin_at:
        row["reason"] = "most recent completed run started before the manifest pin"
    elif created < cutoff:
        row["reason"] = (f"most recent completed run {created.date()} is outside the "
                         f"{window_days:g}-day window")
    elif diagnostic:
        why = "most recent completed run is a diagnostic, focused, or partial run"
        if older_green:
            why += " and supersedes an older green run"
        row["reason"] = why
    elif run.get("conclusion") != "success":
        why = f"most recent completed run conclusion {run.get('conclusion')}"
        if older_green:
            why += " and supersedes an older green run"
        row["reason"] = why
    else:
        row["verdict"] = "green"
        row["reason"] = MANIFEST_PIN_PASS
        row["binding"] = "manifest-pin"
        row["last_green_at"] = run.get("created_at")
        row["last_green_run"] = run.get("html_url")
        # Not the honua-server sha: this run has no receipt that names the candidate.
        row["last_green_build"] = "manifest-pin"


def evaluate(records: dict[str, dict], now: dt.datetime, window_days: float, *,
             candidate: Candidate, advisory: tuple[str, ...] = (),
             unbound: dict[str, str] | None = None,
             pin_at: dt.datetime | None = None) -> dict:
    """Admit each gate from its newest completed run.

    A bound gate counts only the newest run whose report names `candidate`. A gate with no such
    receipt can still pass, and the reason is exactly "pass (manifest-pin bound, no receipt)":
    its newest completed trunk run succeeded, started strictly after `pin_at`, falls inside the
    window, and is not diagnostic or focused where a report says so. An older green never fills
    in for a newer failure. The aggregate is admissible only when every non-advisory gate is green.
    """
    if unbound is None:
        unbound = UNBOUND_GATE_LIMITATIONS
    cutoff = now - dt.timedelta(days=window_days)
    gates = {}
    for name, rec in records.items():
        row = _blank_gate(name, advisory)
        if name in unbound:
            _judge_unbound(row, rec, unbound[name], pin_at, cutoff, window_days)
            gates[name] = row
            continue
        if name == ARC_GATE:
            arm_results = {}
            for arm in ("A1", "A2"):
                classified = []
                for run in rec.get("runs") or []:
                    kind = _classify(run, candidate, name, arm)
                    created = _parse(run.get("created_at"))
                    if kind != "unrelated" and created is not None:
                        classified.append((created, int(run.get("id") or 0), kind, run))
                classified.sort(key=lambda item: (item[0], item[1]))
                if not classified:
                    arm_results[arm] = {"verdict": "red", "reason": "no candidate-bound required-arm run"}
                    continue
                created, _, kind, run = classified[-1]
                good = kind == "qualifying" and created >= cutoff
                arm_results[arm] = {"verdict": "green" if good else "red",
                                    "reason": "pass" if good else "latest run failed or is outside the window",
                                    "run": run.get("html_url"), "created_at": run.get("created_at")}
            row["arms"] = arm_results
            if all(item["verdict"] == "green" for item in arm_results.values()):
                row.update(verdict="green", reason="candidate-bound pass for both required arms",
                           last_green_at=min(item["created_at"] for item in arm_results.values()),
                           last_green_build=candidate.server_sha[:12])
            else:
                row["reason"] = "required arc arm evidence missing or invalid: " + ", ".join(
                    arm for arm, item in arm_results.items() if item["verdict"] != "green")
            gates[name] = row
            continue
        classified = []
        for run in rec.get("runs") or []:
            kind = _classify(run, candidate, name)
            created = _parse(run.get("created_at"))
            if kind == "unrelated" or created is None:
                continue
            classified.append((created, int(run.get("id") or 0), kind, run))
        if not classified:
            row["reason"] = ("no completed run binds this candidate's honua-server digest, "
                             "source SHA, and platform lock")
            gates[name] = row
            continue
        classified.sort()
        created, _run_id, kind, run = classified[-1]
        row["latest_run"] = run.get("html_url")
        row["latest_conclusion"] = run.get("conclusion")
        older_green = any(item[2] == "qualifying" for item in classified[:-1])
        if created < cutoff:
            row["reason"] = f"last candidate-bound run {created.date()} is outside the {window_days:g}-day window"
        elif kind != "qualifying":
            why = "most recent candidate-bound run does not certify this candidate"
            if older_green:
                why += " and supersedes an older green run"
            if any(is_diagnostic(doc) for doc in _documents(run)):
                why += "; diagnostic or focused run"
            if run.get("conclusion") != "success":
                why += f"; conclusion {run.get('conclusion')}"
            row["reason"] = why
        else:
            row["verdict"] = "green"
            row["reason"] = f"candidate-bound pass on trunk {created.date()}"
            row["last_green_at"] = run.get("created_at")
            row["last_green_run"] = run.get("html_url")
            row["last_green_build"] = candidate.server_sha[:12]
        gates[name] = row
    red = sorted(n for n, g in gates.items() if g["verdict"] != "green" and not g["advisory"])
    advisory_red = sorted(n for n, g in gates.items() if g["verdict"] != "green" and g["advisory"])
    limitations = sorted(n for n, g in gates.items() if g["verdict"] == "limitation")
    return {
        "evaluated_at": now.replace(microsecond=0).isoformat(),
        "window_days": window_days,
        "candidate": {
            "server_sha": candidate.server_sha,
            "server_digest": candidate.server_digest,
            "platform_lock": candidate.platform_lock,
        },
        "manifest_pin_at": pin_at.replace(microsecond=0).isoformat() if pin_at else None,
        "manifest_pin_bound": sorted(
            n for n, g in gates.items() if g.get("binding") == "manifest-pin"),
        "admissible": not red,
        "red": red,
        "advisory_red": advisory_red,
        "limitations": limitations,
        "gates": gates,
    }


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def render_markdown(result: dict) -> str:
    candidate = result.get("candidate") or {}
    lines = [
        "| Gate | Verdict | Last green on trunk | Build | Run |",
        "|---|---|---|---|---|",
    ]
    for name in sorted(result["gates"]):
        g = result["gates"][name]
        when = (g["last_green_at"] or "never")[:10]
        run = f"[run]({g['last_green_run']})" if g["last_green_run"] else "—"
        build = f"`{g['last_green_build']}`" if g["last_green_build"] else "—"
        if g["verdict"] == "green":
            mark = "✅"
        elif g.get("advisory"):
            mark = "⚠️"
        else:
            mark = "❌"
        lines.append(f"| `{name}` | {mark} {g['reason']} | {when} | {build} | {run} |")
    identity = ""
    if candidate:
        identity = (f"Candidate honua-server `{candidate.get('server_sha')}` "
                    f"digest `{candidate.get('server_digest')}` "
                    f"platform lock `{candidate.get('platform_lock')}`.\n\n")
    head = (f"Window: {result['window_days']:g} days ending {result['evaluated_at']}. "
            f"Admissible: **{'yes' if result['admissible'] else 'no'}**"
            + (f" — red: {', '.join(result['red'])}" if result["red"] else "") + "\n\n")
    tail = ""
    pinned = [n for n, g in sorted(result["gates"].items()) if g.get("binding") == "manifest-pin"]
    if pinned:
        tail += (f"\nThese gates passed as `{MANIFEST_PIN_PASS}`. That binding is weaker than a "
                 "candidate receipt: the newest completed trunk run was a success, started after the "
                 "platform-manifest pin, inside the window, and was not diagnostic, focused, or partial "
                 "where the workflow exposes that. The receipt each gate does not publish:\n")
        for name in pinned:
            tail += f"- `{name}`: {result['gates'][name].get('limitation')}\n"
    advisory = sorted(n for n, g in result["gates"].items() if g.get("advisory"))
    if advisory:
        tail += ("\nAdvisory (Preview evidence, reported, not required for rc under the 2026-10-25 founder "
                 "re-tier): " + ", ".join(f"`{g}`" for g in advisory) + "\n")
    if result.get("deferred_to_ga"):
        tail += ("\nDeferred to GA (reported, not required for rc): "
                + ", ".join(f"`{g}`" for g in result["deferred_to_ga"]) + "\n")
    return identity + head + "\n".join(lines) + "\n" + tail


def find_pin_time(server_sha: str, manifest: str = "platform-manifest.yaml",
                  cwd: str | None = None) -> dt.datetime:
    """Committer time of the first commit whose honua-server sha field is this candidate.

    Uses `git log -G` on the manifest sha line, oldest first, then parses that commit's file.
    A comment that mentions the sha is not the pin. Raises ValueError when no commit sets the field,
    so a run cannot be treated as having started after a pin that was never found.
    """
    sha = normalize_sha(server_sha)
    if not sha:
        raise ValueError("pin lookup needs a full honua-server sha")
    pattern = f'sha:[[:space:]]*"?{sha}"?'
    log = subprocess.run(
        ["git", "--no-pager", "log", "--reverse", "--format=%H%x09%cI", "-G", pattern, "--", manifest],
        cwd=cwd, capture_output=True, text=True, check=False,
    )
    if log.returncode != 0:
        detail = (log.stderr or log.stdout or "git log failed").strip().splitlines()
        raise ValueError("cannot find the manifest pin commit: " + (detail[-1] if detail else "git log failed"))
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ValueError("PyYAML is required to read platform-manifest.yaml") from exc
    for line in log.stdout.splitlines():
        if "\t" not in line:
            continue
        commit, iso = line.split("\t", 1)
        show = subprocess.run(
            ["git", "--no-pager", "show", f"{commit}:{manifest}"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if show.returncode != 0 or not show.stdout:
            continue
        try:
            data = yaml.safe_load(show.stdout) or {}
        except yaml.YAMLError:
            continue
        server = ((data.get("components") or {}).get("honua-server") or {}) if isinstance(data, dict) else {}
        pinned = normalize_sha(server.get("sha") if isinstance(server, dict) else None)
        if pinned != sha:
            continue
        parsed = _parse(iso.strip())
        if parsed is None:
            raise ValueError(f"pin commit {commit} has no committer timestamp")
        return parsed
    raise ValueError(
        f"no commit pins honua-server sha {sha} in {manifest}; "
        "admission cannot prove a run started after the pin")


def server_identity(path: str) -> tuple[str, str]:
    """honua-server sha and image digest pinned in platform-manifest.yaml."""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise ValueError("PyYAML is required to read platform-manifest.yaml") from exc
    try:
        data = yaml.safe_load(open(path, encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    server = ((data.get("components") or {}).get("honua-server") or {}) if isinstance(data, dict) else {}
    sha = normalize_sha(server.get("sha") if isinstance(server, dict) else None)
    digest = normalize_digest(server.get("digest") if isinstance(server, dict) else None)
    if not sha or not digest:
        raise ValueError(f"{path} components.honua-server is missing a full sha or sha256 digest")
    return sha, digest


def resolve_candidate(server_sha: str | None, server_digest: str | None,
                      platform_lock: str | None, platform_lock_file: str | None,
                      manifest: str) -> Candidate:
    sha = normalize_sha(server_sha) if server_sha else None
    digest = normalize_digest(server_digest) if server_digest else None
    if server_sha and not sha:
        raise ValueError("--server-sha must be 40 lowercase hex characters")
    if server_digest and not digest:
        raise ValueError("--server-digest must be sha256:<64 lowercase hex>")
    if not sha or not digest:
        loaded_sha, loaded_digest = server_identity(manifest)
        sha = sha or loaded_sha
        digest = digest or loaded_digest
    if platform_lock and platform_lock_file:
        raise ValueError("pass only one of --platform-lock and --platform-lock-file")
    lock = normalize_digest(platform_lock) if platform_lock else None
    if platform_lock and not lock:
        raise ValueError("--platform-lock must be sha256:<64 lowercase hex>")
    if platform_lock_file:
        try:
            raw = open(platform_lock_file, "rb").read()
        except OSError as exc:
            raise ValueError(f"cannot read platform lock {platform_lock_file}: {exc}") from exc
        lock = "sha256:" + hashlib.sha256(raw).hexdigest()
    if not lock:
        raise ValueError("--platform-lock is required; admission cannot name a candidate without its platform lock")
    return Candidate(sha, digest, lock)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "honua-io/honua-release"))
    ap.add_argument("--branch", default="trunk")
    ap.add_argument("--window-days", type=float, default=7)
    ap.add_argument("--gate", action="append", default=None,
                    help="workflow file to evaluate (repeatable); default: train gates + AI gates")
    ap.add_argument("--without-ai-gates", action="store_true")
    ap.add_argument("--set", choices=("rc", "ga"), default="rc",
                    help="rc: train gates minus the GA-deferred set, AI gates advisory (default); "
                         "ga: every gate, AI gates included, required")
    ap.add_argument("--server-sha", default=None, help="honua-server source SHA; default: platform-manifest.yaml")
    ap.add_argument("--server-digest", default=None,
                    help="honua-server image digest sha256:<64 hex>; default: platform-manifest.yaml")
    ap.add_argument("--platform-lock", default=None, help="sha256:<64 hex> of the candidate platform-lock.json")
    ap.add_argument("--platform-lock-file", default=None, help="path to platform-lock.json; hashed as the lock digest")
    ap.add_argument("--manifest", default="platform-manifest.yaml")
    ap.add_argument("--out", default="gate-window.json")
    ap.add_argument("--markdown", default=None)
    ap.add_argument("--enforce", action="store_true", help="exit 1 unless every required gate is green")
    args = ap.parse_args(argv)
    if args.window_days <= 0:
        ap.error("--window-days must be positive")
    try:
        candidate = resolve_candidate(args.server_sha, args.server_digest, args.platform_lock,
                                      args.platform_lock_file, args.manifest)
        pin_at = find_pin_time(candidate.server_sha, args.manifest)
    except ValueError as exc:
        print(f"rc gate window: {exc}", file=sys.stderr)
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GH_TOKEN is required (anonymous Actions API calls 404)", file=sys.stderr)
        return 2
    advisory: tuple[str, ...] = ()
    if args.gate:
        gates = tuple(args.gate)
    else:
        gates = TRAIN_GATES + (ARC_GATE, STUDIO_CUSTOMER_GATE) + (() if args.without_ai_gates else AI_GATES)
        if args.set == "rc":
            gates = tuple(g for g in gates if g not in GA_DEFERRED)
            advisory = tuple(g for g in RC_ADVISORY if g in gates)
    now = dt.datetime.now(dt.timezone.utc)
    since = now - dt.timedelta(days=args.window_days)
    records = {}
    for gate in gates:
        try:
            records[gate] = fetch_gate_runs(
                args.repo, gate, token, args.branch, bound=gate in CANDIDATE_BOUND_GATES, since=since)
        except urllib.error.HTTPError as exc:
            print(f"rc gate window: {gate}: GitHub API HTTP {exc.code}", file=sys.stderr)
            return 2
    result = evaluate(records, now, args.window_days, candidate=candidate, advisory=advisory,
                      pin_at=pin_at)
    result["repo"], result["branch"], result["set"] = args.repo, args.branch, args.set
    result["deferred_to_ga"] = list(GA_DEFERRED) if args.set == "rc" and not args.gate else []
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    md = render_markdown(result)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(md)
    print(md)
    if args.enforce and not result["admissible"]:
        print(f"rc gate window: {len(result['red'])} gate(s) red: {', '.join(result['red'])}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
