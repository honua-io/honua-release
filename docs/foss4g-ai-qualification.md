# FOSS4G AI qualification implementation

This guide defines qualification for the bounded pilot profile. Existing RC
gates and the separate 36-scenario/108-prompt/252-case campaign retain their own
contracts. Pilot support or a passing subset does not replace either AI gate.

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

## Qualification coverage

The profile is an inventory with explicit blockers; it does not execute all 134
cases by itself. Qualification needs operational repair, GP-to-map and the
remaining interaction/fault adapters, an authenticated pilot receipt producer,
pilot dispatch integration, a live candidate matrix, cleanup qualification and
five representative user sessions. Component unit tests, build/style checks,
smoke tests and a Console-vendored Studio artifact are supporting evidence.
They require verified artifact provenance and installed-candidate execution
before they can qualify this pilot. Source review and a post-merge audit do not
establish historical pre-merge clearance or installed acceptance.

The [fixed budget amendment](foss4g-pilot-budget.md) preserves the same candidate's
historical costs and liability. Candidate-scoped admissions enforce the
four-active-attempt ceiling and retain stale capacity until cleanup and all
liabilities are settled. The existing controller's stricter single-owner lease
remains; the legacy dispatcher refuses the amended policy rather than funding
an unqualifiable campaign. Pilot dispatch must integrate these primitives before
live execution. Configuration or inventory generation does not authorize a paid campaign.

[Hosted candidate qualification](foss4g-hosted-candidate.md) defines isolated
Console/authentication deployment and installed acceptance requirements. A
healthy standing Server does not establish an installed ECS/Redis Console
candidate or its real human/model sessions.

## Execution and evidence requirements

Record current outcomes against the selected candidate and authenticated
producer evidence. A genuine terminal-model canary is supporting evidence;
do not repeat paid inference merely to refresh a status note. It does not
qualify the cloud deployment or full agent campaign. Environment approval,
successful planning, exact-plan custody and installed execution are separate
steps. A successful unapplied plan is not a deployment receipt, and source
checks or an identity-only enrollment emitter do not qualify human sign-in.

Retain application topology, native operation ceilings, Console-turn adoption,
reviewed Server task outputs and SCIM credential bindings as applicable to the
selected installation. Infrastructure changes use checked-in IaC and reviewed
workflows; historical shell commands do not override that execution boundary.

Preserve the 252-case campaign, the separate 134-case pilot, all original failed
attempts, candidate costs, aggregate reservations and unsettled liabilities.
Readiness requires distinct real author/approver nominations, actual Cognito
sessions, five representative user sessions, complete AI journeys, recovery
and an installed-candidate recording. No issue is complete while it owns an
unsatisfied unique requirement. Report missing evidence as blocked rather than
inferring a pass from source, an old outcome or a supporting fragment.
