# Test fixtures

`server-harness-contract-input.json` provides 42 static protocol assignments for
convergence tests. Its revision and tracking URL identify the fixture. It is not
a captured execution, server attestation, or qualification record.

`capacity-envelope.redis.v1.json` preserves the capacity lock before the Redis-free
component amendment. Checker tests use it to prove historical receipts still require
Redis evidence. Synthetic receipts built from this lock are never qualification proof.
