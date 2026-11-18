# Offline outcome cohort reporting

`tools/outcome_eval.py` records the prospective 192 agent and 24 browser trial
identities and validates individual independently graded transcripts.
`tools/outcome_report.py` replays those transcripts to produce descriptive cohort
results. Neither command admits a live trial, authenticates a grader, dispatches
a model or produces a release-promise PASS receipt. Authenticated producer
provenance, verification of the declared candidate, admission approval and live readback remain
separate release requirements.

Create the ordered plan with `python3 tools/outcome_eval.py plan plan.json`.
The cohort manifest uses this structure (the example abbreviates `trials`):

```json
{
  "schema": "honua.outcome-cohort/v2",
  "candidate": {
    "serverSha": "<40 lowercase hexadecimal characters>",
    "imageDigest": "sha256:<64 lowercase hexadecimal characters>",
    "platformLockDigest": "sha256:<64 lowercase hexadecimal characters>"
  },
  "plan": "plan.json",
  "files": {
    "plan.json": "<SHA256 of exact file bytes>",
    "trial/events.jsonl": "<SHA256 of exact file bytes>",
    "trial/grade.json": "<SHA256 of exact file bytes>",
    "trial/readback.json": "<SHA256 of exact file bytes>"
  },
  "trials": [
    {
      "trialId": "<registered identity>",
      "eligible": true,
      "events": "trial/events.jsonl",
      "grade": "trial/grade.json"
    },
    {
      "trialId": "<registered identity not yet admitted>",
      "eligible": false,
      "reason": "Safe fixture not admitted"
    }
  ]
}
```

The cohort and every recorded grade require the same `candidate` object, with
exactly these three canonical pins. This includes failed, censored, excluded and
NOT RUN grades. A cohort with no recordings still names its intended candidate.
Reports use `honua.outcome-report/v2` and retain that identity alongside the exact
cohort hash. Unbound v1 cohorts are rejected: retain their original records and
prepare a new manifest and independently reviewed grades for v2. Never add pins
to old evidence as a substitute for establishing what was actually deployed.
This consistency check does not authenticate the supplied pins or prove that
readbacks came from that deployment; the protected producer must establish that
through the installed-stack harness and Actions provenance.

`tools/authenticated_producers.py` is that producer. `gate-ai-native.yml` and
`gate-release-outcomes.yml` run it on trunk in the protected `gate` environment,
with the workflow's server SHA, image digest and platform-lock digest. It writes
`honua.release-promise-receipt/v1` receipts only after `gate.verify_installed`
confirms the installed stack and the pinned cohort agrees: every planned trial
is still present, successful readbacks name that stack and candidate, the three
hostile-input channels were blocked, and recovery and unattended timings are
recomputed from pinned evidence. Sentence and click values are medians of the
successful trials only; censored trials stay in the denominator and out of that
median. A missing harness, missing file, changed byte, identity mismatch or
dropped trial removes any receipt and fails the job. The producer does not call
a model or create a stack. Until the shared harness is importable, a dispatch
fails closed and leaves the promise rows without a receipt.

List every identity exactly once: ordered plan `trials` followed by `baselines`.
Eligibility is explicit, including for eligible trials not yet attempted. Missing
recordings require a reason and retain NOT RUN status. Recorded exclusions must
be ineligible; an attempted non-excluded trial must be eligible. Eligibility and
exclusion decisions require independent admission/grading review; hashes alone
do not establish that authority.

The `files` map must pin the plan, every transcript, every grade and every grade's
referenced evidence, relative to the cohort directory. Grade evidence paths stay
relative to the grade directory. Paths cannot escape their containing evidence
root. Freeze and retain the approved admission manifest separately; preserve
each report's exact cohort manifest rather than replacing inputs in place.

Run `python3 tools/outcome_report.py cohort.json report.json`. Output creation
refuses to overwrite an existing file. Any missing identity, changed input,
unregistered plan, mismatched grade or unpinned readback stops reporting. Counts
are recomputed from raw events, ignoring supplied summary values.

The report retains all trial rows and provides 72 workflow/model/arm groups
(including eight browser groups), with three planned repetitions each. It shows
eligibility, attempted and all grade-status counts, success/planned and
success/attempted. Attempted includes an exclusion that has dispatch/stop events;
NOT RUN and pre-dispatch exclusions do not count as attempts. Metric medians and
ranges include all attempts, preserving failure and exclusion costs. Every metric
includes known/missing counts; unknown tokens stay null.

The `passes` list gives the same descriptive counts and attempted metric medians
for each repetition and model, plus a separate browser row per repetition: 32
planned agent trials per model and eight browser baselines. Unattempted passes
remain present with null medians. `stopEvents` counts raw `stop` boundaries,
including attempted exclusions; it does not infer a stop from a grade status or
count a NOT RUN record as a stop. This is a count of recorded trial boundaries,
not a certification of pass-level safety stop decisions.

Browser ratios pair by workflow/repetition and retain both raw counts and agent
click/field costs. A failed pair or zero sentence denominator yields null (NA).
Arm comparisons pair within workflow/repetition/model, with D versus A primary
and B/C versus A secondary. Differences are treatment minus A on successful
pairs only; every pair retains both statuses. Read these beside the full success
denominators. No pooled efficacy, time-saving claim or release verdict is inferred.
