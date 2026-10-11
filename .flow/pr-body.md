fix(resolver): leave the R22 platform-version stamp to mint (#231)
## What breaks today

Every nightly since R22 landed (#433) that selects a bound honua-server or honua-console image refuses itself at `resolve`. Nightly [38066789790](https://github.com/honua-io/honua-release/actions/runs/38066789790) (2026-10-10) ended with:

```
candidate: honua-server.artifactVersion None must be the platform version '2026.1.0-rc.2' of platformRelease '2026.1-rc.2' (R22)
candidate: honua-console.artifactVersion None must be the platform version '2026.1.0-rc.2' of platformRelease '2026.1-rc.2' (R22)
```

Here's the order of events:

1. `resolve()` calls `release_carried_platform_identity`, which removes every carried `artifactVersion`. This is correct: yesterday's version must not name tonight's bytes.
2. `resolve()` then runs `validate_platform.validate(..., exact_candidate=True)`. Its R22 block requires every bound imaged row to carry the label's platform version.
3. The next step, `mint_nightly_lock.py --stamp`, picks the label and stamps the version. The night never gets that far.

The release promise this breaks is #231's acceptance criterion: the nightly train manufactures the signed platform lock (`honua-2026.1.0-rc.N`). While the resolver refuses its own candidate, no lock can be minted. The resolver tests didn't catch it because they all monkeypatch `validate`.

## Fix

- `validate()` and `check_exact_candidate()` take `version_stamped_later` (default `False`). When it is set, a bound imaged row with **no** stamp is accepted. A stamp that is **present but wrong** still refuses.
- Only `resolve_trunk_candidate.resolve` passes it, on both the refusal-path and success-path checks. The flag is not on the CLI.
- No gate is relaxed. The nightly `protocol_ledger` bind step still runs `python tools/validate_platform.py --exact-candidate` on the stamped manifest (`nightly-certification.yml` "Bind the verified ledger and re-check the exact candidate"). Freeze, bind and promotion keep the full R22 rule.

## Evidence

- New `test_the_resolver_does_not_refuse_its_own_unstamped_image[produce|require]` runs the **real** `check_exact_candidate` with the flags the resolver passes, on a bound server row that carries a stale stamp. It fails on trunk with the nightly's R22 refusal and passes with the fix.
- New `test_the_resolver_may_leave_a_bound_image_unstamped_for_mint` and `test_a_wrong_platform_stamp_refuses_even_when_mint_stamps_later` in `test_platform.py`. The existing `test_exact_candidate_rejects_a_bound_image_with_no_platform_stamp` is unchanged and still passes.
- Offline replay on the committed `platform-manifest.yaml`, shaped by `release_carried_platform_identity` (bound imaged row: honua-console):
  - `version_stamped_later=False` → `honua-console.artifactVersion None must be the platform version '2026.1.0-rc.2' ... (R22)`
  - `version_stamped_later=True` → no R22 error
  - after `stamp_platform_version`, the full unflagged check → no R22 error
- `python -m pytest tools/test_resolve_trunk_candidate.py` 190 passed; `tools/test_platform.py` 185 passed; `tools/test_deploy_qualification.py` 26 passed (LF checkout; a CRLF `core.autocrlf=true` checkout fails 5 SDK-baseline digest tests on trunk too).
- `python tools/validate_platform.py` OK.

## What remains for #231 (not in this PR)

After this fix, the same nightly's remaining resolve refusals belong to other owners. They are listed here so nobody reads this PR as closing #231:

- `release/sdk-capability-baseline.json` is missing at the selected revisions of honua-sdk-js, honua-sdk-dotnet, honua-sdk-python and geospatial-mcp. These are WI-5 producer files in those repos, and need an SDK republish.
- `deploy.honua-server.awsLambda.architectures.x86_64` needs a candidate-bound qualification receipt (S3/S4, the ECR mirror and Lambda cell).
- Helm chart publication identity (WI-6) is still `pre-release` with no digest.
- The acceptance criterion "a third party fetches and verifies every artifact without building from source" cannot be shown before a signed rc lock exists.

Refs #231 (released: this removes the resolver's self-refusal only; the remaining lock refusals are cross-repo producer, Lambda-qualification and chart-publication items, and third-party verification needs a minted rc lock)
