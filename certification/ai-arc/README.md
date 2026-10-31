# ARC v1 admission inputs

Admission fails until the release owner commits the founder-approved, nonempty `gating-scope.json`
with `approvedBy` set to `founder` and a timezone-qualified `approvedAt`, and freezes all four
SHA-256 values in `harness-digests.json`. The checked-in preregistration is the current draft;
replace it with the signed-off frozen version before the evidence run. Its exact file bytes define
`preregDigest`. Do not populate approval or digest values from a run being evaluated.

A single completed `ai-arc.yml` run must upload aggregate JSON artifacts whose names contain
`aggregate`, `report`, `receipt`, `evidence`, `identity`, `lock`, or `canary`. It must contain
one aggregate for each approved target and required arm. Besides the preregistration's example,
`stages.studio.assistant` must equal `us.anthropic.claude-sonnet-4-6`, identifying the required
built-in assistant. Each void must name an adjudication, a voided run ID and a replacement run ID
present in the scheduled runs. Each scheduled run needs a distinct nonempty run ID and a SHA-256
receipt digest. The exclusions list is exactly the three entries in the preregistration example,
including the local-qualified exclusion on AWS receipts. Focused or diagnostic reports must set
`certifying: false` or an explicit focused `requestedScope`.
