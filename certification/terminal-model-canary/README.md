# Terminal-model canary harness

This directory defines the harness-only evidence contract for honua-release#161. It does not claim
that a model has completed the 2026.1 journey. Live execution remains sequenced after the deterministic
#123 driver is green against the same candidate.

The harness imports `../terminal-journey/journey.v1.json`; stage numbers, IDs, commands, and milestones
are never copied into the canary. Every receipt records that file's path and SHA-256. The current #123
artifact builds an honestly blocked receipt, but does not expose a live action adapter. #123 must
implement `driver-protocol.v1.json` at its declared
`certification/terminal-journey/live_driver.py` path before this harness can execute to green. The
workflow does not accept an arbitrary executable path.

## Endpoint configuration

The client uses the OpenAI-compatible `POST <base-url>/chat/completions` JSON shape. Configuration is
provider-neutral:

- `TERMINAL_MODEL_BASE_URL` — required for an attempted run; hosted or local/self-hosted URL, normally
  ending in `/v1`.
- `TERMINAL_MODEL_NAME` — endpoint model identifier, including local/open-weight identifiers.
- `TERMINAL_MODEL_API_KEY` — optional bearer credential for hosted/key-based endpoints. Local endpoints
  select authentication `none`, so this hosted credential is neither read nor forwarded. Hosted runs
  select `bearer` and fail closed if the secret is absent. Only the environment-variable reference is
  recorded.
- `TERMINAL_MODEL_RUNTIME` and `TERMINAL_MODEL_QUANTIZATION` — explicit receipt metadata.

Missing endpoint configuration produces a `skipped` receipt and a visible notice. It can never produce
`pass`. A configured endpoint without the #123 adapter produces `blocked`, naming that dependency.
An attempted live run also requires a repository-local path to #123's green receipt plus explicit
runtime and quantization identifiers. The harness parses that receipt, requires a passing roster and
all imported stages, enforces a 24-hour freshness window, and exact-matches its release, server, and
#123 client-artifact pins to the candidate manifest. It records the validated receipt's path and hash;
an arbitrary, stale, blocked, incomplete, or differently pinned receipt fails before model execution.

## Evidence boundary

Every action is attributed as either:

- `MODEL_SELECTED`: a command or tool call parsed from a captured model response; or
- `HARNESS_DRIVEN`: workspace setup, error injection, separate-principal approval, verification, or
  teardown.

Model actions must reference the redacted assistant transcript entry that selected them. The harness
injects one recoverable error through the driver, records the harness action that armed it, the model
action whose driver result reports that exact error ID, and the later model action whose driver result
reports recovery of that ID. Prompts, responses, requests, and results are recursively redacted before
they enter the receipt. Credential values are never included in the prompt or receipt.

The manual workflow supports `ubuntu-latest` for hosted endpoints and `self-hosted` for a locally
reachable endpoint. It has no schedule. Until #123 supplies the live adapter, its honest terminal
state is `skipped` or `blocked`.
