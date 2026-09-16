# Focused Console read/approve receipt (honua-server#3365)

This receipt proves the `admin:read` + `admin:approve` API-key recipe against an imaged server nightly.
The manifest-pinned Console image, signed in as a separate human operator, witnesses the same proposals.
It is the Console half of [honua-server#3365](https://github.com/honua-io/honua-server/issues/3365) and uses the recipe from [honua-console#351](https://github.com/honua-io/honua-console/issues/351).

## What `run.py` boots

Every image is pinned by digest, and `run.py` records every image ID:

- PostGIS and Redis;
- the honua-server image, passed as `--server-digest`. Its `org.opencontainers.image.revision` label must equal `--server-revision`;
- Keycloak as a real OIDC IdP over TLS, with PKCE, a confidential client and one realm user that has the `admin` role and `tenant_id=public`;
- the Console image pinned by the `honua-console` component of `--manifest`. Its baked `HONUA_CONSOLE_COMMIT_SHA` must equal the manifest `sha`. It runs as `Production` in `witness` mode with **no admin API key**;
- a Caddy trusted edge, the only published route to the Console. It replaces the client's identity headers with the operator identity and strips any access token.

`run.py` generates every secret fresh for the run: database password, bootstrap admin password, bearer signing key, IdP client secret, edge secret and operator password. Secrets travel only through process environments: the compose file and realm import carry `${VAR}` placeholders, so no secret is written to disk. Before writing a receipt, the harness checks it and every captured Console page for any generated secret or minted key, and refuses to write if one appears.

## Checks

| Check | #3365 box | What is asserted |
| --- | --- | --- |
| `mintKeysThroughAdminApi` | 1, 2 | Both keys are minted through `POST /api/v1/admin/api-keys`. Each key reads its own `effective-permissions`, which must be exactly the requested grants, `active` and able to authenticate. |
| `adminGetsAuthorizedForReadApproveKey` | 1 | Covers every parameterless GET in the running image's `/api/v1/admin/openapi.json`. Each route is called as full admin, as `admin:read`+`admin:approve` and as `admin:read`. The scoped key must never get 401/403 and must get the full-admin status. |
| `unrelatedAccessPolicyWriteDenied` | 1 | `PUT /api/v1/admin/services/x/access-policy` returns 403 for both scoped keys. |
| `readOnlyKeyDecisionDenied` | 2 | Three real studio-draft deletions are paused behind `RequiresApproval`. `admin:read` approve and reject return 403, the problem detail names `admin:approve`, and the proposal and draft are unchanged. |
| `readApproveKeyDecides` | 1 | The scoped key's approve returns 200 `Succeeded` and deletes the draft. Its reject returns 200 `Rejected` and keeps the draft. `resolvedBy` is the scoped key. |
| `consoleOperatorWitness` | 4 | Before server sign-in, the Console fails closed on the approved proposal (401, nothing rendered). The operator then signs in through `/auth/server/login` → the IdP → the Console-origin callback, and the Console exchanges the server session for its own operator bearer. `/approvals?proposalId=` must render the exact approved, rejected and pending proposal IDs with statuses `Succeeded`, `Rejected` and `AwaitingApproval`. |

Box 3 (tests and vocabulary) is server-side evidence and is cited rather than re-run. At source `2cc22138`, see
`tests/dotnet/Honua.Server.Tests/Features/Admin/ProposalEndpointsTests.cs`,
`tests/dotnet/Honua.Server.Tests/Infrastructure/Authentication/AdminApiKeyPermissionTests.cs` and
`docs/developer/api-specs/admin-api.json`. That spec lists "`admin:read` (safe-method reads), `admin:approve` (read plus proposal approve/reject only), …".
They were delivered by server PRs #3576, #4372 and #4736.

Not exercised: the sealed terminal handoff. The canonical Console producer (`npm run receipt:console`) needs a zero-to-map checkpoint paused at `console-approval` and the sealed Studio handoff `honua.studio.real-model-ai-arc-handoff/v1`. That handoff's producer exists only on unmerged honua-studio#45, and the terminal journey is still blocked (honua-release#122/#123). The receipt records this under `notExercised`; it doesn't simulate the input.

## Receipt on file

`receipt.nightly-2cc2213.console-dcb9eb2.json` was observed 2026-09-16 with:

- server `ghcr.io/honua-io/honua-server:nightly-2cc2213` (`sha256:61e06ef3…d4e51`, source `2cc22138`, dbSchema 120);
- Console `candidate-dcb9eb2b39ed-34879792105-1` (`sha256:37685c71…a619`), the pin proposed in honua-release#357.

**Status: failed on one check.** 109 of the 110 admin GET routes match full admin for the scoped key. `GET /api/v1/admin/jobs` returns 403 to `admin:read` keys, with or without `admin:approve`, but 200 to admin, `admin:write` and `admin:manage` keys. The cause is `OperatorApprovalGate` job-read authorization ignoring the read grant, tracked in [honua-server#4981](https://github.com/honua-io/honua-server/issues/4981). Every other check passed.

Also observed and recorded, but not asserted:

- `executionOperationId` is null on both resolved proposals;
- in witness mode the Console still renders approve controls on the pending proposal. The harness never clicks them;
- the approval inbox carries the "Preview — outside the focused 2026.1 client" banner.

## Run

```sh
docker pull <each image above>   # anonymous GHCR pulls work
python3 certification/console-read-approve/run.py \
  --server-digest sha256:61e06ef3a94d00e4c8fc57ce93e008a5e31b2dcf1da5deb22781fdd42d2d4e51 \
  --server-revision 2cc221388ea47d78c29e79eaee62737e4c792351 \
  --server-tag nightly-2cc2213 \
  --playwright <honua-console>/e2e/playwright/node_modules/@playwright/test \
  --manifest platform-manifest.yaml \
  --output receipt.json
```

The run takes about four minutes, mostly Keycloak start-up. It exits non-zero when any check fails and still writes the failed receipt. `--keep` leaves the stack running for diagnosis. The generated certificates are deleted when the run exits, so a kept server can no longer reach the IdP.
