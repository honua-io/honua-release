# Private security summary gate

`gate-security-private-summary.yml` produces the `security-open-high` release promise.
Dispatch it on `trunk` with `server_sha`, `server_digest`, and `platform_lock_digest` for
one candidate. Use the dedicated dispatch so the receipt belongs to this producer run.

Create the `security-tracker` environment in honua-release. Configure its deployment
branch rule to allow only the `trunk` branch, with no tags. Store these two Actions
secrets in that environment:

- `SECURITY_TRACKER_REPOSITORY`: the private tracker in `owner/repository` form.
- `SECURITY_TRACKER_TOKEN`: a fine-grained token restricted to that tracker, with
  read-only Issues and repository metadata access. The workflow token is not a fallback.

Do not store either secret at repository scope. If repository-scoped copies exist,
move them to the environment and remove the repository copies. The workflow job names
this environment and independently requires the trunk ref; environment branch rules
also prevent modified workflows on other branches from requesting the credential.
Verify the branch rule and secret scopes before the first dispatch.

The query verifies private visibility, enabled issues, and the `severity/high` label,
then reads only the total count of open issues with that label. Maintain that label on
all high findings. The producer never requests finding identifiers, titles, or bodies,
and it suppresses API error content. It refuses redirects with the private credential.

The only uploaded file is `security-open-high.json` in `release-promise-receipts`.
It contains the integer `openHighFindings`, candidate identity, observation time, and
receipt status/assertions. Zero produces PASS; a positive count produces FAIL and fails
the job. Invalid identity, missing configuration, inaccessible or public sources,
missing labels, and malformed/partial API responses fail without a receipt. An earlier
receipt is removed before collection so a failed collection cannot reuse it.

Only an authenticated completed producer run on trunk is release evidence. Running the
script in a lane can debug configuration but cannot certify a candidate. The release
promise consumer validates run provenance, freshness, and exact candidate identity.
