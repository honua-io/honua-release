# Full-matrix execution evidence

The release build/test gate requires an authoritative CI run on the exact pinned server SHA.
A check name or public Actions URL cannot prove that a test executed. The gate reads the run's
actual job inventory through GitHub REST, matches required jobs to the GitHub Actions app and
that run's check suite, and compares every server shard against `.github/ci-shards.json` fetched
at that exact commit. Every expected shard must have a successful actual `Run server test shard`
step; a green job with a swallowed failure, skipped step or missing shard is insufficient.

The selected completed workflow/event and existing five release-critical lanes remain required.
All REST pages must be complete. Job IDs, source SHA, workflow run, check IDs and suite are bound
explicitly. For a failed-only rerun, the gate selects each job's latest actual attempt within the
same run and immutable SHA. Unchanged successful earlier-attempt jobs retain their actual attempt
number; they are never relabelled as having executed in the rerun. A later failed attempt cannot
be hidden by an earlier success, and another run or source cannot supply missing work.

Ordinary current selective runs are already rejected because the required Python integration job
only executes under full CI. This change additionally rejects an unrelated check publisher
impersonating that missing lane with its name and a copied public URL, and independently checks
that the complete pinned server shard selection executed. It does not depend on inferred job
counts or a hardcoded current shard count.

Local execution remains useful diagnostic and regression evidence. Passing JSON from a local
script into a pure verifier is not a trusted producer observation. The release CLI collects from
fixed GitHub API repository/run/source routes; it does not accept a locally asserted execution
inventory. Importing local results for release credit needs a separately governed producer that
binds frozen source, command/selection, environment, exit/results and artifact hashes, plus trusted
runner identity and attestation. Setting GITHUB_* variables or inventing an Actions URL is not
that protocol. This change does not introduce a local attestation/import path or relax release
qualification; that broader audit remains under #652/#376.
