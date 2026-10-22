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
navigation while a real turn is busy, and a dashboard composition fragment.
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
the authenticated pilot receipt producer, controller concurrency enforcement,
live candidate matrix, cleanup qualification and five user sessions remain open.
The dashboard filter change also needs Studio build/test validation, bundle
integration into Console, and installed-candidate execution before it can count.

The [fixed budget amendment](foss4g-pilot-budget.md) preserves the same candidate's
historical costs and liability. The four-concurrent-attempt limit still requires
controller leases before paid dispatch. No paid campaign is started by this change.

Scope overlap was reconciled into existing tickets: #619 retains the broad
campaign, #652 the platform audit, #277 commercial readiness, #377/#480 the
terminal journey and scheduled proofs, #481 canary binding, and #791 toil metrics.
No issue is closed while it still owns an unsatisfied unique requirement.
