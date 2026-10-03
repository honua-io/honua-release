# Component-version declarations

Applies to honua-release#231 WI-3a, under the 2026.1 continuous-certification
plan (#376, R18). The nightly train resolves its own candidate, so no lock field
can be a hand value. Each component repository therefore declares its own
contract and schema versions, and the resolver reads that declaration at the
revision the candidate pins.

## The file

Every repository named by a `components` or `experimental` row of
`platform-manifest.yaml` carries `release/component-versions.json` at its root.
[`schemas/component-versions.v1.schema.json`](../schemas/component-versions.v1.schema.json)
defines it:

```json
{
  "format": "honua.component-versions/v1",
  "component": "honua-console",
  "contractVersions": {"admin": "v1"},
  "schemaVersions": {"diagnostic-bundle": "1.0"}
}
```

- `format` is exactly `honua.component-versions/v1`.
- `component` is the manifest key the repository declares for. A file copied
  from another repository is refused.
- `contractVersions` lists the wire and API contracts the component provides or
  is certified against. For example, `admin: v1` is the `/api/v1/admin` surface.
- `schemaVersions` lists the versioned documents, configuration and storage
  formats the component reads or writes.
- Each value is an exact version string the repository's source already
  carries: a `.v1` file name, a `const` in a JSON Schema, a proto package or a
  `VERSION` file. Placeholders (`TBD`, `pending`), channel words (`latest`,
  `nightly`, `trunk`) and ranges (`^`, `~`, `>=`, `*`) are refused, as they are
  everywhere else in the lock (`tools/component_versions.py`).
- `schemaVersions.database` is reserved. For `honua-server`, the resolver
  derives it from the selected migration tree as `dbSchema`, so `honua-server`
  may declare `schemaVersions: {}`. The candidate then carries
  `{"database": "<floor>"}`.
- Duplicate keys and unknown fields are refused.

## How the resolver reads it

`tools/resolve_trunk_candidate.py` reads the file through `GitHub.file` (the
contents API at `?ref=<sha>`):

- for each `components` row, at the trunk sha it selected that night;
- for each `experimental` row, at the sha the manifest pins.

The declared maps replace whatever the manifest carried. A map from yesterday,
or one typed by hand, never reaches the candidate.

The resolver refuses a component when its file is missing (404), unreadable, not
UTF-8 JSON, does not match the schema, names another component, or declares a
non-exact version. That night's refusal lists every such component at once,
together with any selection failures.

## Empty sets

An explicit `{}` means "this component versions no contract (or schema)". It
is accepted only for a component the manifest marks `sourcePinnedOnly: true`.
The resolver, `tools/validate_platform.py` and `tools/generate_platform_lock.py`
all apply that rule, and the lock records `{}`. An absent map is still "not
declared", even for a `sourcePinnedOnly` component. An empty map on any other
component is refused.

Which components may be `sourcePinnedOnly` is an operator ruling (#231 WI-3),
not a resolver default. Today the manifest marks only `honua-mobile` and
`honua-collect`. If the ruling marks another component, that is a manifest
change; this mechanism needs no code change.
