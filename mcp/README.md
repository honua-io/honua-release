# mcp/

MCP release tools — the eventual interface for AI-coordinated (and later autonomous) release cutting. Thin,
policy-enforcing tools over the GHA release-train surface:

`release.status()` · `release.cut_rc(label, dry_run)` · `release.gate_report(run_id)` ·
`release.promote(label)` · `release.rollback(env, to_label)` · `release.freeze(on|off)`

The MCP server holds the GitHub App credentials + policy (who may cut, freeze flag, required-approval state)
and checks them **server-side** before dispatching. The AI calls tools; it never holds tokens, never touches
gates directly, and cannot bypass the `release-promotion` environment approval. Autonomy expands inward-out
(RC+staging first, prod gated) as gates earn trust.

`release.rollback(env, to_label)` is implemented by `mcp/release_rollback.py`. One invocation creates
one durable parent operation bound to the environment's exact current-lock digest and the retained
target-lock bytes. The parent fans out idempotently over every declared serving target, worker
profile, config projection, capability projection, and the forward-schema compatibility check.

```bash
python mcp/release_rollback.py \
  --environment environment.json \
  --from-lock retained/lock-b.json \
  --to-lock retained/lock-a.json \
  --store operations \
  --receipt rollback-receipt.json
```

Reissuing the same call returns/resumes the same operation without repeating provider mutations.
Changing the target bytes is refused. A divergent or failed plane terminates as
`ManualInterventionRequired` with per-plane recovery data; only exact convergence plus functional
serving/worker/config/capability smoke reaches `Succeeded`. The release-cut certification workflow
executes both restart/success and injected mixed-state paths and signs both candidate-bound receipts.
