# ARC v1 held-out set (sealed)

The held-out goal phrasings for the AI arc admission gate (prereg `PREREG-arc-v1.md`, "Held-out
set"). Only `.github/workflows/ai-arc.yml` decrypts them, in memory, at run time, through
`tools/ai_arc_runner.py` `load_goal_set`. This file records custody facts only. It contains no goal
text, and none may ever be added here.

## Files

| File | Set | Cases | sha256 of the ciphertext |
|---|---|---|---|
| `heldout.v1.sealed` | primary | 30 (20 local-docker, 10 aws-ecs) | `a8c720795dd445a47cef1be8726dd557385456b97df38c9a0f5d0c28255a761a` |
| `heldout-reserve.v1.sealed` | reserve | 6 (6 local-docker) | `3cce9c20680e7e23622c38e04467cb6bb7b539bbfc3ff08f51c8ca111983b8fd` |

Each case is complete: `caseId`, `target`, four stage goals (`setup`, `ops`, `services`, `studio`),
`fixtures` and `fault`. Every goal is at least 40 characters and passes `forbidden_hits` (no harness
or eval vocabulary). All 144 goals are distinct.

## Case counts and the run schedule

- local-docker: 20 primary cases. `schedule` requires a required arm's primary case count to equal
  the prereg run count for the target exactly (`bars.runs` = 20), so the primary set holds 20, not
  more. Reserves (6) replace voided runs only, through `reserve_rows`.
- aws-ecs: 10 primary cases (`bars.runs` = 10), for publication as the AWS lanes land. No reserve.
- Round-trip validated in the authoring process with the repo's own `load_goal_set`, `schedule`
  (A1 and A2 on both targets) and `reserve_rows`.

## Fixtures and faults

- `fixtures` is drawn from the committed corpus fixtures that have frozen expectations for the arc's
  scenarios (`certification/agent-e2e/corpus/manifest.v1.json`, `synthetic-town` variants A and B;
  `certification/agent-e2e/expectations/local/*.json`): `town-A` and `town-B`, alternating. Variant C
  has no frozen arc expectations and is not used.
- `fault` is drawn from the committed request-path fault catalog `tools/agent_e2e_local_faults.py`
  `KINDS`: `http_503_once` and `response_lost_after_commit`, varied across cases.

## Authoring and custody

- Author: an isolated Fable session, model ID `claude-fable-5-1`, on 2026-11-01. It reviewed no PRs,
  and did not open `certification/agent-e2e/pack.v2.json` or any development prompt text (the
  `goals`, `handoffs` and `customerScript` fields of `arc.v1.json` were excluded from every read).
- The manifests were built in memory in one Python process, encrypted with a Fernet key generated in
  that process (`Fernet.generate_key()`), validated by round-tripping the committed ciphertext through
  `load_goal_set` with the key set in that process's environment only, and written to disk as
  ciphertext only. No plaintext manifest, goal text or key was written to disk, printed, or echoed
  in any message.
- The key was passed to `gh secret set AI_ARC_HELDOUT_KEY -R honua-io/honua-release` on stdin from
  the same process and then dropped from memory. The GitHub Actions secret is the only copy.
- Exposure log: `handoffs/evals/heldout-exposure.md` (honua-io workspace). Pack v2 variant C is
  retired and was not consulted.
