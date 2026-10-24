# Authenticate candidate inputs before qualification

The release train supports a preparation-only dispatch for the original candidate
identity needed by the hosted agent campaign. It uses the committed trunk manifest
and matrix, resolves actual artifact facts, applies the same strict pre-cut source
checks as nightly qualification, and attests those two source inputs. The unsigned
qualification lock must regenerate exactly from those authenticated bytes.

Dispatch `release-train.yml` on `trunk` with the manifest's `platformRelease` as
`platform_label`, `dry_run=false`, and `freeze_only=true`. Capacity and DR receipt
URLs are omitted for this mode. The workflow refuses other refs, adopted candidate
refs, dry runs and nightly/preparation combinations. Full release-train dispatches
still require both receipt URLs.

The completed run retains `candidate-manifest`, including the exact qualification
lock and `frozen-lock/preparation-status.json`. Use that actual run and attempt as
the campaign's candidate input; the campaign must authenticate the original
attestations and exact source bytes. Do not replace its verifier or manufacture a
local signed lock.

Preparation runs no qualification gates, creates no cloud resources, signs no
release lock, and publishes no release. Its green workflow result establishes only
input preparation. All mandatory release gates and installed-candidate demo
evidence remain outstanding until their own accepted runs complete. This mode
cannot emit the full train's gate report or certified-candidate artifact.
