# mcp/

MCP release tools — the eventual interface for AI-coordinated (and later autonomous) release cutting. Thin,
policy-enforcing tools over the GHA release-train surface:

`release.status()` · `release.cut_rc(label, dry_run)` · `release.gate_report(run_id)` ·
`release.promote(label)` · `release.rollback(env, to_label)` · `release.freeze(on|off)`

The MCP server holds the GitHub App credentials + policy (who may cut, freeze flag, required-approval state)
and checks them **server-side** before dispatching. The AI calls tools; it never holds tokens, never touches
gates directly, and cannot bypass the `production` environment approval. Autonomy expands inward-out
(RC+staging first, prod gated) as gates earn trust.

Stub — see docs/RELEASE-ENGINEERING-PLAN.md §15. Natural to host alongside geospatial-mcp tooling.
