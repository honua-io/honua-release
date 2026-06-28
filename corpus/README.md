# corpus/ — the AI workflow corpus

The catalog of **operational workflows the AI is allowed to run** — the heart of *safe AI-driven ops*.

The AI does **not** improvise operations. It selects and runs a workflow from this corpus, and every
workflow here is:

1. **declaratively specified** — `preconditions → steps → verify`, machine-readable;
2. **rollback-safe** — it carries a `rollback` procedure **and** a rollback `verify`, so the AI can
   prove it can undo what it did. *A workflow with no tested rollback is not AI-runnable* — the gate
   refuses it (you don't hand an autonomous operator an action it can't reverse);
3. **integration-tested** — it points at a scenario in `e2e/operational/` that exercises the workflow
   *and its rollback* against a real candidate;
4. **autonomy-tiered** — `autonomy_tier` (1/2/3, per RELEASE-ENGINEERING-PLAN §15) declares how far the
   AI may run it unattended:
   - **1** — staging only; a human promotes to prod.
   - **2** — prod, but only behind a canary/SLO gate that must stay green N minutes (human can veto).
   - **3** — fully autonomous on cadence; humans on the kill switch (`release.freeze`) + auto-rollback.

`tools/check_corpus.py` validates every workflow against this contract and **fails closed** — a
workflow missing a rollback, a verify, or a real integration test cannot enter the corpus. That is the
enforceable guarantee behind "AI proposes, the pipeline disposes": the AI can only run vetted,
reversible, tested operations, and the gate can't lie about which those are.

## Workflows
| id | category | tier | rollback |
|---|---|---|---|
| `publish-service` | data-ops | 2 | unpublish + confirm gone |
| `upgrade-version` | platform-ops | 1 | downgrade to prior pinned release + confirm |
| `gp-deploy` | gp-ops | 2 | remove the deployed GP tool + confirm |
| `gp-execute` | gp-ops | 2 | revert the job's writes (or no-op for read-only) |
| `cut-release-candidate` | release-ops | 1 | discard the RC (no promote) |
| `promote-release` | release-ops | 1 | rollback the platform label to the prior release |

Each `corpus/workflows/<id>.yaml` is the source of truth; the MCP release tools (`mcp/`) expose these
as the only operations the AI may invoke.
