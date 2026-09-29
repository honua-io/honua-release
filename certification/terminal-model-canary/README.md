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

The client uses the candidate server's `POST <base-url>/v1/studio/ai/chat` signed SSE
proxy. For the #377 promise journey, configure a provider named `bedrock` with
adapter kind `bedrock`; the harness checks that capability, explicitly selects it,
and requires signed Claude-on-Bedrock provenance. It never calls a provider directly.

- `TERMINAL_MODEL_BASE_URL` — candidate API base URL (normally ending in `/api`),
  or the complete `/v1/studio/ai/chat` URL. Plain HTTP is restricted to loopback;
  redirects and direct provider URLs are refused before credentials are sent.
- `TERMINAL_MODEL_NAME` — exact Claude model identifier accepted by the candidate's
  Bedrock adapter: an AWS-controlled `anthropic.claude-*` foundation model ID or a
  system inference profile ID prefixed with `us.`, `eu.`, `apac.`, or `global.`
  (for example, `us.anthropic.claude-sonnet-4-6`). Operator-controlled aliases,
  custom/imported model ARNs, and application inference profiles cannot certify the
  model family and are rejected. Supply IDs rather than ARNs. See the
  [AWS model ID documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/models-get-info.html).
- `TERMINAL_MODEL_API_KEY` — optional bearer credential for hosted/key-based endpoints. Local endpoints
  select authentication `none`, so this hosted credential is neither read nor forwarded. Hosted runs
  select `bearer` and fail closed if the secret is absent. Only the environment-variable reference is
  recorded.
- `TERMINAL_MODEL_RUNTIME` and `TERMINAL_MODEL_QUANTIZATION` — explicit receipt metadata.
- `TERMINAL_MODEL_SIGNING_MANIFEST_SHA256` — required platform-controlled SHA-256 of the canonical
  candidate transcript-signing manifest. Candidate-published keys are accepted only when that
  manifest matches this independently configured trust anchor.

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
reports recovery of that ID. Prompts, responses, requests, and results are recursively redacted;
the receipt stores only their SHA-256, UTF-8 byte count and `digest-only` marker.
The schema rejects raw payloads and extra fields. Redacted model context stays in
memory for subsequent turns and is never replayed from receipt digests. Each live run
uses a cryptographically random nonce, signed provider-event bytes bind both SSE names and data, and
the signed reported model must exactly match the requested model.

The manual workflow supports `ubuntu-latest` for hosted endpoints and `self-hosted` for a locally
reachable endpoint. It has no schedule. Until #123 supplies the live adapter, its honest terminal
state is `skipped` or `blocked`.
