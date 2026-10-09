# mcp/

There is no MCP server in this directory, and none is built for 2026.1. No `release.*` MCP tools exist.
The only tool here is `mcp/release_rollback.py`, an argparse command-line program run directly or by
workflows. It holds no GitHub credentials and enforces no cut/promote/freeze policy; release gates live
in GitHub Actions (`release-train.yml`, `nightly-certification.yml`) and the `release-promotion`
environment approval.

`mcp/release_rollback.py` performs a lock-to-lock rollback. One invocation creates
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
