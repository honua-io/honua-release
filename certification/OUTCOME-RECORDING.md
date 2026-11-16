# Workflow outcome recording

`tools/outcome_eval.py` prepares a prospective trial order and measures recorded
events against an independent grade. It makes no provider, browser or product
requests. Its output is an offline measurement, not an authenticated gate receipt
or proof that the grader's assertions are true.

```sh
python3 tools/outcome_eval.py plan ordered-plan.json
python3 tools/outcome_eval.py verify-pins preparation-pins.json
python3 tools/outcome_eval.py summarize events.jsonl grade.json summary.json
python3 -m unittest discover -s tools -p test_outcome_eval.py
```

Outputs use exclusive creation: retain existing records and choose a new output
path for another measurement. The pin manifest contains a nonempty `files` object
mapping relative paths to lowercase SHA256 hashes. Paths must remain within its
directory, including after resolving symlinks.

## Admission and identity

Before evaluated execution, approve the prospective registration and freeze the
resolved model versions, component revisions, tool hashes, arm prompts and
knowledge indexes, fixture restore procedure, trial namespaces, negative probes,
recorder settings and matched browser scripts. Missing admission evidence means
NOT RUN. Generating an order or checking hashes does not grant admission.

The order has eight workflows, two model labels, four knowledge arms and three
repetitions: 192 agent trials paired with 24 browser baselines. Arms rotate through
each position equally per model. The plan retains requested model labels; record
resolved versions separately before execution. `upload-publish` denotes the
publish-layer workflow. Agent trials also record any person clicks and field work.

## Raw events

Each JSONL event has a unique nonempty `id`, nonnegative finite monotonic `seconds`
from the recorder's clock, and a `type`. Keep failed attempts and retries.

| Type | Additional fields and meaning |
| --- | --- |
| `dispatch` | Start of evaluated work; exactly once |
| `terminal-readback` | Successful terminal readback; exactly once |
| `stop` | Failed, censored or excluded run boundary; exactly once |
| `person` | Visible `text` and explicit `sentences` partitioning that text; includes initial brief and follow-ups |
| `click` | `count` of 1 or 2, including double-clicks and retries |
| `field` | Stable `fieldId` and nonnegative integer `characters` entered in that action |
| `tool-attempt` | One attempted tool call, including failed attempts |
| `tokens` | Unique `responseId`; integer `input`, `output`, `cache`, or null when unavailable |
| `knowledge-read`, `navigation`, `scroll`, `retry`, `observer` | Retained context; adds no action count |

Do not turn navigation into fabricated clicks. A retry also needs its actual click,
field or tool-attempt events to count the effort. Token components stay null if any
response omits that component; unavailable usage never becomes zero. Only observer
events may fall outside the dispatch-to-terminal/stop interval.

## Independent grading

The grade is a JSON object containing `trialId`, registered `workflow`, `modality`
(`agent` or `browser`), `grader`, `status`, and `evidence`. Evidence maps every
supporting relative file to its SHA256 and must include the exact raw transcript.
Optional `pass`, `model`, `arm` and `baselineId` must agree with the planned trial.

Statuses are `success`, `failed`, `censored`, `excluded` and `not-run`. Every
non-success needs a reason. Success additionally requires
`postconditionsPassed: true`, `controlsPassed: true`, and `terminalReadback`
naming a hashed independent readback. The grader verifies the actual product
state and hostile-input controls; model prose and shell exit status cannot supply
that evidence. Any person text requires `sentenceBoundariesReviewed: true`.

Dispatch and terminal readback determine successful elapsed time. Other evaluated
runs require dispatch and stop; stop and terminal readback cannot coexist. More
than ten person sentences, 100 tool calls, or the plan's workflow time limit
requires `censored` or `excluded`. Failure costs remain in the summary. Empty
not-run/excluded records remain identifiable with null elapsed time, preserving
the denominator. Never relabel such records as successful release receipts.

Authenticated producers must separately satisfy `RELEASE-PROMISE.md`, bind the
candidate pins and Actions provenance, and use the shared installed-stack harness.
This counter does not dispatch that harness or certify its observations.
