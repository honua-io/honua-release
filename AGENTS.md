# AGENTS.md — honua-release

**NEVER ADD AI/AGENT ATTRIBUTION TO ANY COMMITS OR PR/ISSUE BODIES** — no `Co-Authored-By`, no
"Generated with Claude/Codex", no robot emoji. Author every commit as the repo owner
(Mike McDougall <mike@honua.io>). Write plain, descriptive messages.

This repo is the platform release-engineering home. Core invariants:
- **AI proposes, the pipeline disposes.** Release gates live in GitHub Actions and must be able to *fail*; they
  cannot be overridden by a human or AI. A gate that can't fail is worse than no gate.
- **The platform manifest + compatibility matrix are the source of truth** for what versions ship together.
- **Do not grant AI release autonomy until the gates can't lie** (real gates + real telemetry — Phase 1).

See `docs/RELEASE-ENGINEERING-PLAN.md` and `docs/TEST-STRATEGY.md`.
