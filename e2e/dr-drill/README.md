# Local-docker disaster-recovery drill

`run.sh` is the executed DR certification path for the GA local-docker topology. It starts the exact
server image digest in `platform-manifest.yaml`, seeds customer-shaped state, creates the supported
PostgreSQL custom-format backup, removes the database volume, restores into a new volume, and starts
the server against the restored database.

A pass requires byte-stable logical checksums and row counts for layers/features, jobs/logs, alerts,
audit, outbox, and sync cursors; the migration journal floor; isolation between two tenant schemas;
and the canonical customer service-catalog browse journey after restore. The receipt measures RPO
from the last seed commit to backup completion and RTO from destruction start to the green journey.

Run `bash e2e/dr-drill/run.sh`. Set `HONUA_DR_SIGNING_KEY` to an operator-controlled Ed25519 private
key; otherwise the local run creates an ephemeral key beside the receipt. CI retains the signed
receipt, detached signature, public key, backup digest, and GitHub artifact attestation.
