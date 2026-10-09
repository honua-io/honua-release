# 2026.1 installation licensing settings

2026.1 ships with licensing disabled. No license file, minting, edition selection,
license envelope or serving-unit band is required. All catalog entitlements are
active; serving-unit bands are neither measured nor enforced. Authentication,
authorization, resource safety limits and capability maturity still apply.
Alerting and offline sync remain Preview. Multi-tenancy is internal (ruling R29):
it runs only on Honua's own demo stack and is not offered for customer deployment.

Use the signed platform lock for the server image and component identities. Save
the following self-contained customer quickstart as `compose.yaml`. The default
is the server image `platform-manifest.yaml` pins for 2026.1 (`nightly-87966c3`,
by digest); when certifying a newer signed platform
lock, set `HONUA_SERVER_IMAGE` to its digest-pinned server image.

<!-- doc-run: file=compose.yaml -->
```yaml
services:
  db:
    image: postgis/postgis:16-3.4
    environment:
      POSTGRES_USER: honua
      POSTGRES_PASSWORD: honua
      POSTGRES_DB: honua
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U honua -d honua"]
      interval: 3s
      timeout: 3s
      retries: 30

  honua:
    image: ${HONUA_SERVER_IMAGE:-ghcr.io/honua-io/honua-server@sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a}
    depends_on:
      db:
        condition: service_healthy
    environment:
      ASPNETCORE_ENVIRONMENT: Development
      ConnectionStrings__DefaultConnection: Host=db;Port=5432;Database=honua;Username=honua;Password=honua
      HONUA_ADMIN_PASSWORD: ${HONUA_ADMIN_PASSWORD:-change-this-admin-key}
      Security__ConnectionEncryption__MasterKey: ${HONUA_MASTER_KEY:-change-this-to-a-64-character-hexadecimal-secret-before-starting}
      Licensing__Mode: Disabled
      HostValidation__AllowedHosts: "*"
      AllowedHosts: "*"
    ports:
      - "8080:8080"
    healthcheck:
      test: ["CMD-SHELL", "wget -qO- --header='X-API-Key: $${HONUA_ADMIN_PASSWORD}' http://localhost:8080/api/v1/admin/license | grep -Eq '\"mode\"[[:space:]]*:[[:space:]]*\"disabled\"'"]
      interval: 5s
      timeout: 5s
      retries: 60
      start_period: 20s
```

Set unique secrets, then start the database and server. `--wait` returns nonzero
unless both services become healthy, including the authenticated disabled-mode
check in the `honua` healthcheck.

```sh
export HONUA_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export HONUA_MASTER_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up -d --wait
```

For another deployment template, put this setting in the **server container's**
environment, and in each standalone worker's environment:

```yaml
Licensing__Mode: Disabled
```

The release repository's local Docker and DR compose files already set it.
On AWS ECS and Lambda, set the typed Terraform input `licensing_mode = "Disabled"`
on the honua-iac example root (the release parity cells pass `-var=licensing_mode=Disabled`);
the example roots do not expose `additional_env`. A host shell
variable alone does not configure a container. Do not set `Licensing__DevGrantEdition`;
the supported disabled mode works in Production and needs no development grant.
Retain the template's required authentication, database and encryption secrets.

After readiness, authenticate with the installer-provisioned admin key and
repeat the same assertion through the server container:

```sh
docker compose exec -T honua sh -c \
  'wget -qO- --header="X-API-Key: $HONUA_ADMIN_PASSWORD" http://localhost:8080/api/v1/admin/license' \
  | grep -Eq '"mode"[[:space:]]*:[[:space:]]*"disabled"'
```

Missing `mode`, an enabled mode or an HTTP failure is non-passing. A pre-ruling
image may ignore the setting: use a candidate containing server#4721, then repeat
installation certification. The release gates enforce the same runtime assertion.

The bundle's `platform-release.v1.json` includes `licensing.mode: disabled`,
`allCatalogEntitlementsActive: true`, `editionGating: false` and
`capacityMetering: false`. Site publication and support claims for 2026.1 must use
these facts and must not advertise license enforcement or edition gating.
See the [operating envelope](2026.1-operating-envelope.md) for qualification limits.
