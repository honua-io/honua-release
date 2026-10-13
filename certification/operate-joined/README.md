# Joined operate proof

The opt-in `operate_joined` input on `e2e-local-docker.yml` drives a real server
through a deterministic deployment drift finding, sealed proposal, authenticated
published Admin CLI read, refused self-approval, separate authorized CLI approval,
self-hosted typed deployment, and a completed 45-second protection window.

Supply the existing `parity_server_image` and `parity_server_source` inputs as an
immutable Honua server registry digest and its full OCI source revision. The
producer verifies those bytes and the manifest-selected public npm packages before
starting the fixture. It does not change release pins or mint qualification.

The reviewed protected-recovery fixture comes from honua-devops commit
`28a6419fcc9bbce6ef9503ff2b7ba37f47fbf7ee`. A private copy receives a random owned
Docker namespace, durable Redis settings, target telemetry defaults, and a declared
fixture release. Both serving and worker declarations use the fixture workload's
actual image ID; this scenario executes only serving deployment. Healthy telemetry
is explicitly simulated by the existing Prometheus fixture. Workload serving,
proposal storage, audit, deployment reconciliation, and elapsed observation are real.

The initial prior revision is deployed to terminal success through the API to
populate the real terminal index. No finding, proposal, workflow state, or audit row
is inserted directly. Retained evidence joins finding, proposal, separate principals,
actual CLI responses, immutable workload IDs, deployment transitions, durable audit,
and final proxy readback. Cleanup checks owned containers and network removal.

This is an automated fixture approval, not human approval acceptance, ECS placement,
worker execution, or an end-to-end release qualification. A failed stage leaves the
report failed and the issue open. Private keys, credentials and configuration stay
outside the uploaded allowlist; only sanitized report and execution log are retained.
