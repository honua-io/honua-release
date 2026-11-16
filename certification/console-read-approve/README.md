# Console read/approve harness

`run.py` checks the `admin:read` + `admin:approve` API-key recipe against a supplied server image. The manifest-pinned Console, signed in as a separate human operator, witnesses the same proposals. A passing focused run covers only the checks below; it does not qualify the complete terminal-to-Console journey.

## What `run.py` boots

Every image is pinned by digest, and `run.py` records every image ID:

- PostGIS and Redis;
- the honua-server image, passed as `--server-digest`. Its `org.opencontainers.image.revision` label must equal `--server-revision`;
- Keycloak as a real OIDC IdP over TLS, with PKCE, a confidential client and one realm user that has the `admin` role and `tenant_id=public`;
- the Console image pinned by the `honua-console` component of `--manifest`. Its baked `HONUA_CONSOLE_COMMIT_SHA` must equal the manifest `sha`. It runs as `Production` in `witness` mode with **no admin API key**;
- a Caddy trusted edge, the only published route to the Console. It replaces the client's identity headers with the operator identity and strips any access token.

`run.py` generates every secret fresh for the run: database password, bootstrap admin password, bearer signing key, IdP client secret, edge secret and operator password. Secrets travel only through process environments: the compose file and realm import carry `${VAR}` placeholders, so no secret is written to disk. Before writing a receipt, the harness checks it and every captured Console page for any generated secret or minted key, and refuses to write if one appears.

## Checks

| Check | What is asserted |
| --- | --- |
| `mintKeysThroughAdminApi` | Both keys are minted through `POST /api/v1/admin/api-keys`. Each key reads its own `effective-permissions`, which must be exactly the requested grants, `active` and able to authenticate. |
| `adminGetsAuthorizedForReadApproveKey` | Covers every parameterless GET in the running image's `/api/v1/admin/openapi.json`. Each route is called as full admin, as `admin:read`+`admin:approve` and as `admin:read`. Both scoped keys must never get 401/403 and must get the full-admin status. New receipts retain every route's three statuses in `responses`, including successful `/api/v1/admin/jobs` reads. |
| `unrelatedAccessPolicyWriteDenied` | `PUT /api/v1/admin/services/x/access-policy` returns 403 for both scoped keys. |
| `readOnlyKeyDecisionDenied` | Three real studio-draft deletions are paused behind `RequiresApproval`. `admin:read` approve and reject return 403, the problem detail names `admin:approve`, and the proposal and draft are unchanged. |
| `readApproveKeyDecides` | The scoped key's approve returns 200 `Succeeded` and deletes the draft. Its reject returns 200 `Rejected` and keeps the draft. `resolvedBy` is the scoped key. |
| `consoleOperatorWitness` | Before server sign-in, the Console fails closed on the approved proposal (401, nothing rendered). The operator then signs in through `/auth/server/login` → the IdP → the Console-origin callback, and the Console exchanges the server session for its own operator bearer. `/approvals?proposalId=` must render the exact approved, rejected and pending proposal IDs with statuses `Succeeded`, `Rejected` and `AwaitingApproval`. |

The focused harness does not exercise the sealed terminal handoff. The canonical Console producer (`npm run receipt:console`) requires a zero-to-map checkpoint paused at `console-approval` and the sealed Studio handoff `honua.studio.real-model-ai-arc-handoff/v1`. A complete journey needs that actual producer input and independent evidence; the focused harness records this limitation under `notExercised` and does not simulate it.

Matching full-admin HTTP statuses checks authorization equivalence, not endpoint health. Parameterized and resource-specific GETs are outside this sweep. Inspect the retained route statuses and any stream errors separately.

## Run

Supply the approved immutable image digest and matching source revision. Use the manifest for the Console pin and an installed Playwright test package:

```sh
python3 certification/console-read-approve/run.py \
  --server-digest <approved-server-image-digest> \
  --server-revision <matching-source-revision> \
  --server-tag <matching-server-tag> \
  --playwright <honua-console>/e2e/playwright/node_modules/@playwright/test \
  --manifest platform-manifest.yaml \
  --output <private-evidence-directory>/receipt.json
```

The harness exits nonzero when any check fails and still writes the failed receipt. `--keep` leaves the stack running for diagnosis. Generated certificates are deleted when the run exits, so a kept server can no longer reach the IdP. Store runtime receipts and captured pages in private evidence storage rather than the public repository.

The `fixtures/` directory contains synthetic status-contract examples for unit tests. They record no execution evidence.
