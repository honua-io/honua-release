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
| `adminGetsAuthorizedForReadApproveKey` | 1 | Covers every parameterless GET in the running image's `/api/v1/admin/openapi.json`. Each route is called as full admin, as `admin:read`+`admin:approve` and as `admin:read`. Both scoped keys must never get 401/403 and must get the full-admin status. New receipts retain every route's three statuses in `responses`, including successful `/api/v1/admin/jobs` reads. |
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

## Refreshed receipt

[`receipt.nightly-80e23be.console-dcb9eb2.json`](receipt.nightly-80e23be.console-dcb9eb2.json)
was observed 2026-09-19 against the newest successful published nightly when the run started:

- server source `80e23bedfe8ff7b43362c8d8ea22bfae1756df7d`, image
  `sha256:9869f044b1c5d0de15aef6c87cc3d60383037ee9a4346d08bb9c83f2c56cc176`;
- the same manifest-pinned Console `dcb9eb2` artifact used in the original receipt;
- [nightly publication](https://github.com/honua-io/honua-server/actions/runs/35333311726).

**All six implemented check groups passed.** Both scoped keys matched full admin on all
110 parameterless GET routes, with zero 401/403 responses. `/api/v1/admin/jobs` returned
200 for all three credentials, closing the observed authorization regression from #4981.
Approval deleted the real draft; rejection preserved the other draft. The separate operator's
Console session rendered those exact proposal IDs as `Succeeded` and `Rejected`, and a third
as `AwaitingApproval`. Before sign-in it returned 401 without rendering the proposal.
The receipt and captured pages passed the generated-credential leakage check.

This is an authorization comparison on a minimal fixture: 92 GETs returned 200, one 204,
five 400, two 402, eight 404 and two 500, identically for all three credentials. The two
500 responses are `/api/v1/admin/share/traffic` and `/api/v1/admin/share/traffic/series`.
The configuration discovery stream also ended early after its 200 response. These are
recorded observations, not claims that every admin endpoint is functionally healthy or
that parameterized/resource-specific GETs were swept.

The sealed terminal handoff remains **unexercised**. Studio #45 is still an open draft at
`0d3920d19a3246439a76b8705947c076df0f7732`; its producer is absent from the default branch.
The candidate exists, so this is an upstream handoff dependency rather than an absent-candidate
deferral. This receipt does not close #3365 or qualify the complete terminal-to-Console journey.
The server image was selected under the September 16 newest-imaged-nightly ruling; this change
does not re-pin the platform server or change any release/support claim.

The browser run uses the host's existing Chromium dependencies. On this lane they are under
`/home/mike/.cache/chromelibs/root/usr/lib/x86_64-linux-gnu`, supplied through `LD_LIBRARY_PATH`.
An initial attempt failed to launch Chromium because `libnspr4.so` was not on its search path;
the complete proof above was rerun after verifying browser startup with that dependency path.

## Original receipt

`receipt.nightly-2cc2213.console-dcb9eb2.json` was observed 2026-09-16 with:

- server `ghcr.io/honua-io/honua-server:nightly-2cc2213` (`sha256:61e06ef3…d4e51`, source `2cc22138`, dbSchema 120);
- Console `candidate-dcb9eb2b39ed-34879792105-1` (`sha256:37685c71…a619`), the pin proposed in honua-release#357.

**Status: failed on one check.** 109 of the 110 admin GET routes match full admin for the scoped key. `GET /api/v1/admin/jobs` returns 403 to `admin:read` keys, with or without `admin:approve`, but 200 to admin, `admin:write` and `admin:manage` keys. The cause is `OperatorApprovalGate` job-read authorization ignoring the read grant, tracked in [honua-server#4981](https://github.com/honua-io/honua-server/issues/4981). Every other check passed.

That defect was fixed in [honua-server#4990](https://github.com/honua-io/honua-server/pull/4990).
Its auth unit and real HTTP integration tests prove scoped job list/detail reads while denying
non-admin reads and read-key cancellation. The original failed receipt remains unchanged as
the before-fix evidence.

Also observed and recorded, but not asserted:

- `executionOperationId` is null on both resolved proposals;
- in witness mode the Console still renders approve controls on the pending proposal. The harness never clicks them;
- the approval inbox carries the "Preview — outside the focused 2026.1 client" banner.

## Run

```sh
docker pull <each image above>   # anonymous GHCR pulls work
python3 certification/console-read-approve/run.py \
  --server-digest sha256:9869f044b1c5d0de15aef6c87cc3d60383037ee9a4346d08bb9c83f2c56cc176 \
  --server-revision 80e23bedfe8ff7b43362c8d8ea22bfae1756df7d \
  --server-tag nightly-80e23be \
  --playwright <honua-console>/e2e/playwright/node_modules/@playwright/test \
  --manifest platform-manifest.yaml \
  --output receipt.json
```

The run takes about four minutes, mostly Keycloak start-up. It exits non-zero when any check fails and still writes the failed receipt. `--keep` leaves the stack running for diagnosis. The generated certificates are deleted when the run exits, so a kept server can no longer reach the IdP.
