# Release promise receipts

`release-promise.v1.json` is the producer contract for the founder's 14 November
2026 22:50 PT amendment to the 13 November release gates. RC requires the ten
runs-itself rows, hardened default, zero open high findings and the cloud install.
AI journeys and injection evidence are reported for RC and required for GA.
Unproven AI journeys must ship marked Preview; a green RC gate does not certify them.

The workflow names in the contract reserve producer interfaces. They do not claim
that those workflows exist or that any live test has passed. Missing producers,
missing receipts, incomplete API history and unavailable credentials fail closed.
The aggregate only reads Actions evidence; it does not deploy or run paid evaluations.

Each producer runs from its repository's `trunk` through `workflow_dispatch`,
`schedule` or `repository_dispatch` and uploads a `release-promise-receipts`
artifact. Each row has its own JSON file at the artifact root, named by `receipt`
in the contract. The Actions API authenticates the repository and workflow;
receipt-supplied provenance is never trusted. The latest completed execution
and every overlapping execution must pass. A newer failure without a receipt
invalidates an older pass, even when the newer run targeted a different candidate.
Re-run the producer for the intended candidate in that case.

Example receipt (the row's own assertions must match the contract):

```json
{
  "schema": "honua.release-promise-receipt/v1",
  "row": "cloud-launch-stack",
  "status": "PASS",
  "serverSha": "<40 hexadecimal characters>",
  "imageDigest": "sha256:<64 hexadecimal characters>",
  "platformLockDigest": "sha256:<64 hexadecimal characters>",
  "observedAt": "2026-11-15T06:00:00Z",
  "stackName": "honua-gate-example",
  "assertions": {
    "live": true,
    "orchestrator": "ecs",
    "redis": false,
    "defaultImage": "postgis-only"
  }
}
```

`observedAt` must fall inside the authenticated run's execution and the last seven
days. All three candidate identifiers must match together. Hardened-profile and
cloud rows require a `honua-gate-` stack name. The security count producer must
run with private tracker read permission in a private-scoped job and publish only
`openHighFindings` (integer zero) and `assertions: {"privateScoped": true}` plus
the common identity fields. Never upload tracker titles, bodies or issue IDs.
The aggregate needs only Actions read permission for these sanitized artifacts,
not private tracker access. No private tracker producer is implemented here.

Run the gate on trunk with `server_sha`, `server_digest`,
`platform_lock_digest` and `profile` (`rc` or `ga`) workflow inputs. Cross-repository
producer artifacts require `RELEASE_GH_TOKEN` with Actions read access; the default
token suffices for producers in honua-release. The summary artifact is
`release-promise-report`; its JSON lists every row, requirement and status, and
binds the candidate. RC admission includes this gate without a manifest-pin
fallback. GA admission additionally requires all five AI rows. Admission rechecks
row freshness at its own evaluation time.

The founder's 14 November 23:00 PT outcomes ruling also requires all 18 timing
measurements in `outcomes` at RC. `gate-release-outcomes.yml` is the reserved
producer interface. It uploads one common-schema receipt per metric with numeric,
finite, nonnegative `value`, `unit: "minutes"`, and assertions `live: true` and
`releaseNotesReported: true`. The same artifact must contain `release-notes.md`
with an exact line for each value, for example:

```markdown
<!-- honua-outcome:install-console --> 12.5 minutes
```

The collector reads the archived notes itself; receipt-supplied note text is
ignored. The producer must archive the release notes used for publication.
The aggregate reports each measured value and whether its GA target passed;
values alone do not certify any live promise row. RC requires the measurements
and notes, without enforcing timing targets. GA additionally requires every
`gaMaximum` to be set and met. The unattended target is zero person-minutes;
other targets remain unset until approved, so they cannot silently pass GA.
No timing measurement or target is fabricated by the gate.
