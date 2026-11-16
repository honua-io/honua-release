# Offline outcome cohort reporting

`tools/outcome_eval.py` records the prospective 192 agent and 24 browser trial
identities and validates individual independently graded transcripts.
`tools/outcome_report.py` replays those transcripts to produce descriptive cohort
results. Neither command admits a live trial, authenticates a grader, dispatches
a model or produces a release-promise PASS receipt. Authenticated producer
provenance, candidate binding, admission approval and live readback remain
separate release requirements.

Create the ordered plan with `python3 tools/outcome_eval.py plan plan.json`.
The cohort manifest uses this structure (the example abbreviates `trials`):

```json
{
  "schema": "honua.outcome-cohort/v1",
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

Browser ratios pair by workflow/repetition and retain both raw counts and agent
click/field costs. A failed pair or zero sentence denominator yields null (NA).
Arm comparisons pair within workflow/repetition/model, with D versus A primary
and B/C versus A secondary. Differences are treatment minus A on successful
pairs only; every pair retains both statuses. Read these beside the full success
denominators. No pooled efficacy, time-saving claim or release verdict is inferred.
