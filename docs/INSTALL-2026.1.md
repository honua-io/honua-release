# 2026.1 installation licensing settings

2026.1 ships with licensing disabled. No license file, minting, edition selection,
license envelope or serving-unit band is required. All catalog entitlements are
active; serving-unit bands are neither measured nor enforced. Authentication,
authorization, resource safety limits and capability maturity still apply.
Multi-tenancy, alerting and offline sync remain Preview.

Use the signed platform lock for the server image and component identities. The
release bundle includes `compose.licensing-disabled.yml`; apply it with the
customer quickstart compose from that server revision (service `honua`):

```sh
docker compose -f docker-compose.yml -f compose.licensing-disabled.yml up -d
```

For another deployment template, put this setting in the **server container's**
environment, and in each standalone worker's environment:

```yaml
Licensing__Mode: Disabled
```

The release repository's local Docker and DR compose files already set it.
AWS ECS/Lambda parity passes it through Terraform `additional_env`. A host shell
variable alone does not configure a container. Do not set `Licensing__DevGrantEdition`;
the supported disabled mode works in Production and needs no development grant.
Retain the template's required authentication, database and encryption secrets.

After readiness, authenticate with the installer-provisioned admin key and verify:

```sh
curl --fail --silent --show-error -H "X-API-Key: $HONUA_ADMIN_PASSWORD" \
  "$HONUA_BASE_URL/api/v1/admin/license" | jq -e '.data.mode == "disabled"'
```

Missing `mode`, an enabled mode or an HTTP failure is non-passing. A pre-ruling
image may ignore the setting: use a candidate containing server#4721, then repeat
installation certification. The release gates enforce the same runtime assertion.

The bundle's `platform-release.v1.json` includes `licensing.mode: disabled`,
`allCatalogEntitlementsActive: true`, `editionGating: false` and
`capacityMetering: false`. Site publication and support claims for 2026.1 must use
these facts and must not advertise license enforcement or edition gating.
See the [operating envelope](2026.1-operating-envelope.md) for qualification limits.
