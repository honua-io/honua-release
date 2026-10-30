# 2026.1 patch backports

Trunk is where a patch is written. `release/2026.1` receives only a reviewed cherry-pick.
[honua-release#380](https://github.com/honua-io/honua-release/issues/380) requires that branch
after an accepted RC and before GA. Creating it is not part of the backport workflow, and it is
not done while 2026.1-rc.2 is being cut.

## Founder or release-session step

This is the whole branch creation. Run it once, on the accepted RC commit, then protect the branch
(pull request required, the `admit` job of `backport-release.yml` required, no force-push, no deletion):

```sh
git fetch origin
git checkout -b release/2026.1 <accepted-rc-commit>
git push origin release/2026.1
```

`backport-release.yml` exits without creating the branch when `origin/release/2026.1` is missing.
It prints this step instead.

## Opening a backport

On a full trunk commit:

```sh
gh workflow run backport-release.yml --ref trunk -f sha=<40-character trunk sha>
```

The workflow checks the commit is on trunk, cherry-picks it onto a `backport/<sha12>` branch, and
opens a pull request into `release/2026.1` with the `backport/2026.1` label. It does not run
`release-train`, `promote`, `rc-release`, or `e2e-cloud-aws`.

## Admission

A pull request into `release/2026.1` fails `admit` unless:

- the label `backport/2026.1` is present, and
- `git cherry -v origin/trunk` marks every commit as already equivalent to a trunk commit.

A commit that exists only on the maintenance branch is not a patch. Privileged release workflows
stay on trunk. The patch profile checker in `tools/patch_promotion_policy.py` is unchanged: a
short-burn promotion still needs the published `honua-2026.1.0` baseline, and this flow does not
mint one.
