# Customer installation artifact profile

`customer-install-manifest.json` records the published artifacts used by the
Windows PowerShell and Linux customer guides. The public site publishes these
same bytes at <https://honua.io/data/customer-install-manifest.json>, so a customer
does not need access to this release-engineering repository.

This is an explicitly **pre-cut rehearsal** profile, not a signed release lock
or a change to `components.honua-server` / the certification ledger. The server
image comes from the successful Windows rehearsal in
[server PR #4429](https://github.com/honua-io/honua-server/pull/4429); its anonymous
OCI index digest and amd64 source label were rechecked on 2026-09-08. Both Honua
Python clients are pinned in `platform-manifest.yaml`; their wheel identities
and the MCP transport wheel come from the linked version-specific PyPI JSON.
The admin source revision is the published `python-admin-v0.1.8` release target.
Alternative npm and NuGet pins are copied from the platform manifest; they are
not required by the Python journey and are not new compatibility claims.

Public GHCR, npm and PyPI artifacts in this profile require no package-read
credential. Only the optional `Honua.Sdk` GitHub Packages feed requires a GitHub
account with package access and a classic PAT scoped to `read:packages` (authorize
organization SSO if required). Do not require GitHub login for the Python journey.

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
