# GA workflow toil experiment

This opt-in experiment records counters from the terminal-model canary without
changing the required rc receipt, release pins, or promotion gates. It does not
prove that the spec pathway exists, that a trial reset its fixtures, or that a
feature is ready for GA. The existing canary still establishes execution evidence.

Supply `--toil-config experiment.json --toil-output artifacts/toil-trial.json`
alongside the existing `tools/terminal_model_canary.py` arguments. The configuration
has exactly these fields:

```json
{"experimentId":"workflow-pilot-1","arm":"imperative","trial":1}
```

The `spec` arm is refused before setup unless the reviewed driver protocol
advertises it, and setup must attest the requested mode. The installed driver
currently implements only `imperative`; a complete comparison is therefore a
future qualification activity, not a result of adding these counters.

The counter-only sidecar retains actual candidate/model/fixture/config/auth-policy
bindings and the source receipt digest. It contains no transcript, tool arguments,
responses, or operation names. Tokens not reported by the verified provider are
null. A model retry is a reissue of a previously failed tool (or identical terminal
command); malformed-action repairs are counted separately. Nested calls made by a
terminal command are not counted as MCP tool calls. Approval duration measures the
separate-principal harness call, not a person's review time.

Clarifications, validation-specific repairs, transport retries and human review
time remain explicitly unmeasured until the corresponding driver events exist.
Console telemetry must remain separate and must not invent external-client token
counts.

For each environment, execute five trials per arm with identical verified model
settings and candidate/fixture/config/auth-policy pins. Alternate arm order and
reset each trial to the same fixture state using the reviewed harness. Keep all
failed and blocked attempts; do not replace them with successful retries. Capture
fixture-reset evidence separately with the experiment records. Run demo trials
only in a dedicated test namespace; this tooling neither provisions nor modifies
a demo deployment automatically.

Compare one environment at a time:

```bash
python3 tools/workflow_toil.py artifacts/toil-*.json --output artifacts/toil-comparison.json
```

The report rejects mismatched pins, duplicate arm/trial identifiers, malformed
counters, and contradictory passing stages. Missing trials produce `incomplete`
and exit 1. Reports include completion/failure counts and per-stage medians over
all observed attempts, with token-observation coverage. A complete cohort is not
a claim of improvement or statistical significance. Review source receipts and
fixture-reset evidence before making a product claim.

No live trials were executed as part of this implementation.
