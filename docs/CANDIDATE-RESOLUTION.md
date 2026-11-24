# Candidate resolution

`tools/resolve_trunk_candidate.py` resolves the component set declared by
`platform-manifest.yaml`. Use `--dry-run` to inspect selection without publishing.
A successful resolution selects inputs; it does not establish installed-product or
live qualification proof. Candidate publication and qualification follow
[the release cut guide](RELEASE-CUT-CHECKLIST.md).

## Check classification

`certification/check_build_test.py` keeps the latest attempt of each check name for
one app and one Actions workflow. A later success replaces an older failure of that
same lane; a later failure replaces an older success. Actions suites without a
recorded workflow identity remain independent, so missing metadata cannot hide a red.

A check remains core unless the release policy explicitly classifies its exact name
or workflow path as env-gated, roll-up, security, governance or advisory. A workflow
exclusion applies only when enrichment recorded that workflow path. If every check is
excluded, the component is blocked. Policy is declared in
`certification/advisory-checks.yaml` and loaded by
`certification/build_test_policy.py`.

Name-only check metadata cannot certify server execution. Strict server acceptance
also requires actual Actions job observations for every required shard in the
immutable pinned inventory. Missing evidence refuses the candidate even when classified core checks
are green. Synthetic examples in `tools/fixtures/candidate-classification/` test these
rules; they are not qualification records.

## Component declarations

A trunk commit whose `release/component-versions.json` returns a contents API 404
is skipped and the walk continues. Other declaration errors refuse resolution.
A missing declaration at a pinned revision also refuses; it is never silently skipped.

## Image selection

Images must bind to the selected revision through their image configuration.
For a Lambda OCI index, the resolver selects the single `linux/amd64` child manifest
and still requires the revision label, architecture and operating system. An
`unknown/unknown` attestation descriptor is not another architecture. An index with
an unsupported platform shape is refused.

The customer server image is native-AOT with PostGIS. JIT images support builds,
tests and verification cohorts. `components.honua-server.imageLabels` in
`platform-manifest.yaml` declares the required variant, checked on every platform:

- `required`: `honua.runtime.compilation` must be `native-aot`.
- `whenDeclared`: `honua.store.profile` must be `postgis` when the image declares it.
  Older images without a store profile label remain eligible.

A tag whose platforms violate these rules is not pinned. A commit with no conforming
tag is skipped with the rejected label recorded in `skips.json`, and the walk
continues. Components without `imageLabels` retain their existing binding rules.
The same variant rules apply to the Lambda image and the certified-nightly prior
selected by `tools/compose_upgrade_prior.py`, so upgrade tests use the customer variant.
