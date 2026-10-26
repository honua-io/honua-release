# gitsign tag fixture

`foreign-workflow-signed-tag.pack` is a git pack holding one real, publicly logged
Sigstore keyless tag signature and the objects needed to serve it over git:

| object | id |
|---|---|
| annotated tag `v0.8.2` | `8cf0333e2c43016cb5fe064c182f005e9f5f26f6` |
| target commit | `db70f65248fb342258f693054138fa2fb353b3cb` |
| the commit's 20 trees (no blobs) | |

Source: `https://github.com/ozeranskii/httptap`, tag `v0.8.2`, signed with gitsign by
`https://github.com/ozeranskii/httptap/.github/workflows/release.yml@refs/heads/main`
(issuer `https://token.actions.githubusercontent.com`, Rekor log index 3244071886).
Built with `git pack-objects` from a `--depth=1` fetch of that tag.

It is a genuine signature by a GitHub Actions workflow that is **not** Honua's promote
workflow. The tests use it both ways: it verifies when the policy names its true identity,
and it is refused when the policy names
`https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk`.
A live signature by Honua's own identity can only be produced inside promote.yml.
