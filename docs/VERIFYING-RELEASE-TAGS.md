# Verifying a Honua release tag

Every Honua platform release is published from an annotated Git tag in
`honua-io/honua-release` named `honua-<version>` (for example `honua-2026.1.0`). The tag
is signed with [gitsign](https://github.com/sigstore/gitsign), Sigstore's keyless Git
signing. No Honua employee holds a signing key. The signature is made inside the
`promote.yml` GitHub Actions workflow, on `trunk`, after the release-promotion approval
and every release gate have passed. Sigstore's Fulcio CA issues a short-lived certificate
for that workflow's OIDC identity, and the signature is recorded in the public Rekor
transparency log.

A tag is genuine only if all three of these hold:

| claim | expected value |
|---|---|
| certificate identity (SAN) | `https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk` |
| OIDC issuer | `https://token.actions.githubusercontent.com` |
| Rekor entry | present and matching the signature |

A valid Sigstore signature by any other identity, such as another repository, another
workflow, another branch, or a person's email address, is **not** a Honua release
signature.

## With gitsign (recommended)

Install gitsign 0.17.1 or later
([releases](https://github.com/sigstore/gitsign/releases)). Then fetch the tag and verify
it:

```sh
git clone --filter=blob:none --no-checkout https://github.com/honua-io/honua-release.git
cd honua-release
git fetch origin tag honua-2026.1.0 --no-tags

gitsign verify-tag honua-2026.1.0 \
  --certificate-identity "https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

A genuine tag prints:

```text
gitsign: Good signature from [https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk](https://token.actions.githubusercontent.com)
Validated Git signature: true
Validated Rekor entry: true
Validated Certificate claims: true
```

and exits 0. An unsigned tag, a lightweight tag, a tampered tag, or a signature by any
other identity exits non-zero.

Use `--certificate-identity` with the exact value above. Do not use
`--certificate-identity-regexp`: a loose pattern would accept signatures from other
workflows.

Then check that the tag names the commit the release notes give as the certified source:

```sh
git rev-parse 'honua-2026.1.0^{}'
```

Do not rely on `git verify-tag` alone. With `gpg.x509.program=gitsign` it checks only that
*some* Sigstore certificate signed the tag. It does not check *whose* certificate it was.

## With cosign: the release assets

gitsign signs the Git tag. `cosign` verifies the files attached to the GitHub Release,
which the same workflow signs keyless with the same identity. That includes
`signed-tag-receipt.json`, the verifier's record of the tag object, target commit and
Rekor log index that promotion checked.

```sh
gh release download honua-2026.1.0 --repo honua-io/honua-release \
  -p 'signed-tag-receipt.json*' -p 'bom.cdx.json*' -p 'finalized-manifest.yaml*'

for f in signed-tag-receipt.json bom.cdx.json finalized-manifest.yaml; do
  cosign verify-blob "$f" --bundle "$f.bundle" \
    --certificate-identity "https://github.com/honua-io/honua-release/.github/workflows/promote.yml@refs/heads/trunk" \
    --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
done

# The receipt must name the tag object you fetched and verified above.
jq -r .tagObject signed-tag-receipt.json
git rev-parse honua-2026.1.0
```

## Release-candidate locks

Each release candidate (`2026.1-rc.N`) is frozen as a lock tag, `nightly-lock/2026.1-rc.N`.
It names the commit that holds exactly the candidate's `platform-lock.json`, its Sigstore
bundle and the BOM. The nightly certification workflow that minted the candidate signs
that tag with gitsign too, under its own identity:

```sh
git fetch origin tag nightly-lock/2026.1-rc.3 --no-tags
gitsign verify-tag nightly-lock/2026.1-rc.3 \
  --certificate-identity "https://github.com/honua-io/honua-release/.github/workflows/nightly-certification.yml@refs/heads/trunk" \
  --certificate-oidc-issuer "https://token.actions.githubusercontent.com"
```

Lock tags minted before this signing was introduced are lightweight and carry no signature.
A repository ruleset forbids updating or deleting any `refs/tags/nightly-lock/**` tag, so a
lock cannot be moved after it is published.

The GitHub pre-release tags that `rc-release.yml` creates are lightweight and unsigned. They
are not release artifacts.

## For maintainers

The trust policy is `certification/release-controls/tag-signing-policy.json`. The producer
and verifier is `tools/tag_signing.py` (`sign` and `verify-remote`), and promotion runs
it in `.github/workflows/promote.yml`. Promotion refuses to create a Release unless the
published tag verifies against the identity above and points at the certified commit.
Regression tests that use a real Sigstore signature are in
`tools/test_tag_signing_gitsign.py`.
