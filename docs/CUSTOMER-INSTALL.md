# Customer installation artifact profile

`customer-install-manifest.json` records the published artifacts used by the
Windows PowerShell and Linux customer guides. The public site is required to copy
these bytes to <https://honua.io/data/customer-install-manifest.json>. Until that
URL publishes the file successfully, use the manifest in this repository; do not
represent the site copy as available.

This is an explicitly **pre-cut rehearsal** profile, not a signed release lock
or a change to `components.honua-server` / the certification ledger. The server
image is the one `platform-manifest.yaml` pins as `components.honua-server`
(`nightly-87966c3`, by digest), which contains the licensing-disabled mode
([release issue #416](https://github.com/honua-io/honua-release/issues/416)). Its
anonymous OCI index digest and amd64 source label were rechecked on 2026-10-05.
The licensing-disabled runtime assertion has not been run against this profile,
so it does not claim candidate or clean-Windows qualification. Both Honua Python clients are pinned in `platform-manifest.yaml`; their wheel identities
and the MCP transport wheel come from the linked version-specific PyPI JSON.
Both Python source revisions are the commit in the PyPI publication certificate of
the pinned wheel. Alternative npm and NuGet pins are copied from the platform manifest;
they are not required by the Python journey and are not new compatibility claims.

Every artifact in this profile is public and requires no package-read credential:
GHCR, npm, PyPI, and the optional .NET client `Honua.Sdk` 1.10.3, which installs
anonymously from nuget.org (`dotnet add package Honua.Sdk --version 1.10.3`; no
GitHub Packages feed, PAT, or `nuget.config` source is needed). Do not require
GitHub login for any journey.

## Start the pinned server

This quickstart requires Docker with the Compose plugin and Python 3. Save the
following file as `compose.yaml`. It uses the digest-pinned server from the
customer manifest. When checking a newer signed release lock, set
`HONUA_SERVER_IMAGE` to that lock's digest-pinned server image before starting.

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
      HONUA_ADMIN_PASSWORD: ${HONUA_ADMIN_PASSWORD}
      Security__ConnectionEncryption__MasterKey: ${HONUA_MASTER_KEY}
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

Generate unique values for both required secrets, start the services, and wait
for the authenticated licensing check in the server healthcheck to pass:

```sh
export HONUA_ADMIN_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export HONUA_MASTER_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
docker compose up -d --wait
```

Confirm the running server still reports the release's disabled licensing mode.
This uses the tools already present in the server image and does not require
`curl` or `jq` on the host:

```sh
docker compose exec -T honua sh -c \
  'wget -qO- --header="X-API-Key: $HONUA_ADMIN_PASSWORD" http://localhost:8080/api/v1/admin/license' \
  | grep -Eq '"mode"[[:space:]]*:[[:space:]]*"disabled"'
```

When updating this profile, verify registry bytes and run
`python3 tools/validate_customer_install_manifest.py`. The required `validate` check
runs the same command: it validates the file against
`schemas/customer-install-manifest.v1.schema.json`, rejects qualification flags a
pre-cut rehearsal cannot claim, binds the rehearsal server image to its registry
manifest and never lets it half-match `components.honua-server`, and fails when
any Honua client version, wheel/package digest, filename, repository or source
commit differs from `clientArtifacts`. Only a passing commit may be copied
unchanged into the site `data/` directory.
Record this repository's immutable commit and the file SHA-256 in the site's
publication record. Update the literal guide pins in the same delivery.

After the release cut, obtain the signed release lock, update the server and
compatible clients together, and run the complete install/import/publish/query,
diagnostics, restart/recreation and scoped-teardown journey on a **clean Windows
machine in the Windows licensed lane**. Link the rehearsal record on
[server #4300](https://github.com/honua-io/honua-server/issues/4300). That validation
is separate from this docs packet; leave #4300 open until it succeeds.
