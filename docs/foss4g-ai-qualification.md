# FOSS4G AI qualification implementation

The coordinating ticket is [honua-release#378](https://github.com/honua-io/honua-release/issues/378).
The accepted scope and schedule are in [the interaction plan, section 7](plans/agent-console-interaction-plan.md).
Existing RC gates and the 36-scenario/108-prompt/252-case campaign keep their own contracts.

## Canonical inventory and component projection

`certification/agent-e2e/foss4g-pilot.v1.json` owns the five journeys, sixteen
interaction templates, prompts, independent assertions, source mappings and
explicit subcases. Expansion yields 134 cases: 57 local Docker and 77 ECS with
Redis; 40 journey cases and 94 interaction cases. The two entrypaths are Console
and the supported external Claude Code client. Journeys use A locally and A/B/C
on ECS. Probes use A with their explicitly applicable entrypaths on both targets.

```bash
python3 tools/agent_e2e_pilot.py validate
python3 tools/agent_e2e_pilot.py inventory --output /private/pilot-inventory.json
python3 tools/agent_e2e_pilot.py export --entrypoint console --output /private/console-pilot.json
python3 tools/agent_e2e_pilot.py report --output /private/pilot-report.json
```

Use a private controller directory in place of `/private`. The export contains
development prompts A/B; C remains in the controller-owned source and must not
enter component or worker environments. These commands perform no paid calls.
The report deliberately exits 1 while pilot-specific independent producers and
live evidence are missing. It cannot accept a self-reported passing result.
Authenticated evidence from the original campaign can support mapped assertions,
but cannot qualify new Console/external-client behavior.

Console consumes the generated projection via `HONUA_FOSS4G_PROFILE_PROJECTION`
in `e2e/playwright/playwright.interactions.config.ts`. The development browser
cases cover natural selected-map references, visual/agent/visual editing,
navigation while a real turn is busy, and a dashboard composition journey.
The dashboard path covers exact rows/aggregates and linked filters, save/reopen,
an exact-version publication proposal, distinct approval, and an anonymous
audience's map/chart/table/filter interactions. The controller operates actual
human-principal sessions for this integration path; it does not establish an
observed human review or replace the representative user sessions.
They require the installed candidate, real model and real signed-in human state.
Recorded attachments are supporting observations, not authenticated pilot receipts.

The native Docker runner adds guided external-client map/save/reopen continuation,
human publication and an independent audience witness in the same retained
external session. See [its instructions](../certification/native-docker-first/README.md).
Its tool-directed orchestration is integration evidence; it does not satisfy the
pilot's natural, goal-only prompt requirement.

## Remaining qualification work

The profile is an inventory with explicit blockers. It does not execute all 134
cases. Operational repair, GP-to-map and the remaining interaction/fault adapters,
the authenticated pilot receipt producer, pilot dispatch integration,
live candidate matrix, cleanup qualification and five user sessions remain open.
Authoring and published-audience controls, including consistent widget/control
alias resolution and teardown cancellation, passed hosted Studio validation at
`dabd2c6`: 1,592 unit tests, typecheck/build/style and all smoke jobs. Console
`7237e8c` contains that provenance-verified artifact and passed its normal hosted
core, E2E and pinned-server integration checks. Bounded-count follow-up
[Studio #124](https://github.com/honua-io/honua-studio/pull/124) keeps informational
notices separate from rendering errors; at `fda6681`, all 1,598 unit tests and
hosted typecheck/build/style and all smoke jobs pass. Console `41d8cc1` vendors
that verified bundle and passes 93 focused checks; its normal CI and Studio
sequential review are pending. The original PR merged during second review; the separately labeled
post-merge audit does not establish historical pre-merge clearance.
Installed-candidate execution is required before these checks can qualify the pilot.

The [fixed budget amendment](foss4g-pilot-budget.md) preserves the same candidate's
historical costs and liability. Candidate-scoped admissions enforce the
four-active-attempt ceiling and retain stale capacity until cleanup and all
liabilities are settled. The existing controller's stricter single-owner lease
remains; the legacy dispatcher refuses the amended policy rather than funding
an unqualifiable campaign. Pilot dispatch must integrate these primitives before
live execution. No paid campaign is started by this change.

[Hosted candidate preparation](foss4g-hosted-candidate.md) records the verified
existing AWS demo and the isolated Console/authentication deployment work. The
healthy Lambda server at `demo.honua.io` does not establish an installed ECS/Redis
Console candidate or its real human/model sessions.

Scope overlap was reconciled into existing tickets: #619 retains the broad
campaign, #652 the platform audit, #277 commercial readiness, #377/#480 the
terminal journey and scheduled proofs, #481 canary binding, and #791 toil metrics.
No issue is closed while it still owns an unsatisfied unique requirement.

## Current execution status — 2026-10-23 20:55 UTC

The [current canonical release checkpoint](https://github.com/honua-io/honua-release/issues/378)
and [interaction plan](plans/agent-console-interaction-plan.md#712-execution-checkpoint--2026-10-23-2055-utc)
retain the original acceptance requirements. Shared application topology #870 and
original foundation history #872 are merged. Exact native operation ceilings
#873 and Console-turn adoption #871 remain under CI/review. The identity-only
enrollment emitter at `5d1019cd` passed 290 Python checks and actionlint; its
complete deployment integration still needs the reviewed Server task output and
SCIM credential binding. None of these source checks qualifies human sign-in.

The last measured RC result remains 18/20: AWS cloud and the full agent campaign
are incomplete. The genuine terminal-model canary passed on original run
40001824669; do not repeat paid inference merely to refresh a status note.
Original cloud deployment39942695095 still awaits genuine environment approval.
Custody PLAN40014973402 is successful and unapplied. Separate exact-plan custody
and foundation phases are being implemented and reviewed before deployment.

Preserve the 252-case legacy campaign, the separate134-case pilot, all original
failed attempts and the $334 aggregate reservations. Distinct real author/approver
nominations, actual Cognito sessions, five user sessions, complete AI journeys,
recovery and the installed-candidate recording are still outstanding. The runbook
remains at `/home/mike/honua-io/RC1-RUNBOOK-20261022.md`; its historical commands do
not override the requirement to configure infrastructure only through IaC and
checked workflows.
