# First complete local Docker execution

The full execution ran all 35 declared documents and 201 fenced blocks against the manifest candidate. The runner returned 1 because published docs failed; candidate boot, health, licensing-disabled assertion, and fixture seed succeeded. There were no unexpected runner exceptions, infrastructure failure rows, pending blocks, or inventory drift.

`full-run-original.json` preserves the original execution at runner revision `05edb2d7f8ca5d499c257f0fd762d7c202f6b3e1`. During that run R29 merged a prose change to `docs/INSTALL-2026.1.md`. After rebasing onto `origin/trunk`, `r29-install-rerun/report.json` records a fresh execution of that guide at `6c6dcf6ed4a96c944a6d326803bee0a325313330`. Its block hashes are unchanged. `report.json` combines the 34 unchanged rows with that actual rerun row; its provenance records the operation. Every final document revision and block hash matches the regenerated inventory.

Document outcomes: 24 fail, 1 needs-input, 10 pass. Block outcomes: 93 fail, 6 needs-input, 79 pass, 23 not-run (preexisting document intent classifications). 169 blocks reached an executor. The zero-block customer install guide fails both the nothing-executed check and its unevaluated candidate-image check.

The local commands used separate work directories and guard ports, keeping published commands unchanged:

```sh
COMPOSE_PROJECT_NAME=honua-execdocs421 E2E_SERVER_PORT=18180 \
E2E_BASE=http://localhost:18180 HONUA_PUBLIC_BASE_URL=http://localhost:8080 \
EXECDOCS_GUARD_PORT=18769 python3 certification/executable-docs/run.py \
  --boot --network candidate \
  --evidence-uri https://github.com/honua-io/honua-release/pull/421 \
  --workdir /home/mike/honua-io/execdocs421-final-local-run \
  --output certification/executable-docs/evidence/first-local-run/report.json \
  --summary certification/executable-docs/evidence/first-local-run/summary.md

EXECDOCS_GUARD_PORT=18770 python3 certification/executable-docs/run.py \
  --only honua-release-docs-install-2026-1 \
  --evidence-uri https://github.com/honua-io/honua-release/pull/421 \
  --workdir /home/mike/honua-io/execdocs421-r29-install-rerun \
  --output certification/executable-docs/evidence/first-local-run/r29-install-rerun/report.json \
  --summary certification/executable-docs/evidence/first-local-run/r29-install-rerun/summary.md
```

The first command's report is preserved before applying the one-document refresh. The candidate and all published client pins match current trunk. `boot.json`, `licensing.json`, and `seed-manifest.json` are receipts from the complete execution. `bootstrap-check/` holds the earlier independent cold-bootstrap verification of the TCP database probe.

The required nightly verdict remains fail. Owning-repository issues are recorded in `issues.json`; each contains all failing and needs-input blocks plus document checks.
