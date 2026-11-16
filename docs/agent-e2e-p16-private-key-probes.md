# P16 private credential transport and item probes

These controller-only components provide credential transport and item probes.
They do not register a supported scenario backend, activate a campaign, change
the frozen 252-case pack or qualify any P16 case.

`agent_e2e_key_secret.OperationKeyConsumer` consumes exactly one native operation
secret under the original task API-key principal and workspace/turn. It first
reads the native target and completed `admin.api-key.create` handle, and requires
its exact single `key` reference. The Server enforces the secret's original
principal, tenant and operation relationship. A distinct independent observer
credential cannot stand in for that authority.

The consume POST bypasses ordinary response capture because that response carries
the plaintext secret. A durable create-only intent binds candidate, campaign,
case, attempt, handle and reference before sending. Requester transport aliases
cannot bypass this guard. No redirects or automatic retries are allowed; bounded
reads share the current lease and deadline. Any uncertain outcome requires
reconciliation of the original operation, including after process loss. Only a
nonsecret consumed outcome is retained, with no body, body hash, headers or error
preview. The secret is held in a nonserializable object in memory, never an
environment variable or evidence file. Once known, it screens earlier durable
records and later participating transports. Closing the object disables further
probes; Python memory clearing does not guarantee cryptographic zeroization.

`agent_e2e_key_probe.KeyPermissionProbe` checks one explicit owned OGC item. It
uses the same issued credential for GET 200, PUT 403, and a later GET 401; a
separate observer compares protected feature content before/after the write and
again after authentication stops. The replacement must change the item. Neither
helper issues keys, revokes them, treats controller probes as model actions, or
accepts 403 as proof of revocation. A caller must independently establish actual
model issuance/revocation, native approvals, exact scoped permissions, owned
fixture identity, that the target publication permits updates, and that the
replacement satisfies the frozen fixture schema. A generic 403 alone is not
proof of the exact issued key's scoped grants. The caller must also prove bootstrap retirement and fresh worker custody. No report from
these helpers alone satisfies those obligations or the frozen permission oracle.

## Runtime qualification prerequisites

A complete native MCP producer must prove model-selected issuance and revocation
through a transport-neutral typed approval/proposal path under the original caller's
authority. Resolve any issued secret privately through its original-principal native
handle. A typed approval-required refusal or an output without secret references
cannot substitute for these observations.

The HTTP operation submission route separately evaluates its operator approval gate.
Terminal access does not prove an authorized approval channel, and raw admin
issuance/revocation cannot substitute for typed model operation evidence. Do not
upgrade worker authority or introduce controller revocation to complete the proof.
If the installed candidate cannot execute the required path, report it unsupported.

Loopback regressions prove transport/probe behavior only. Their simulated state
transitions are not evidence that a current model or Server can complete P16.
