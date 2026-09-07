# Release-line control rollout — #236

This is a **blocked rollout proposal**, not evidence that #236 is complete. The
2026-09-06 Windows recheck after PR #287 merged finds CODEOWNERS in honua-release,
none in the other 16 repositories, and only one human collaborator
(`mikemcdougall`) in every one of the 17 repositories. GitHub does not permit an
author to approve their own PR. Another human must have write access **and** appear
in CODEOWNERS; adding a reviewer who is not an owner does not satisfy the rule.
The accompanying CODEOWNERS change establishes ownership in honua-release; it
cannot manufacture an independent approving human.

The current-versus-required inventory was posted before any settings changes:
https://github.com/honua-io/honua-release/issues/236#issuecomment-5557114421
No live settings have been changed. #236 remains must-fix-before-cut.

## Windows verification after merge

The fresh `snapshot-2026-09-06-windows.json` records each repository's exact
default-branch SHA, observed controls, and observation time. Its companion
`audit-2026-09-06-windows.json` remains **fail for all 17 repositories**. The release
promise still unmet is the adopted quality contract sections 4.4, 10.1 and 14:
reviewed release lines with required checks, no unreviewed bypass, and signed
native publication tags where applicable. Merging the proposal did not activate
these controls or provide an independent reviewer.

The four missing check denominators remain IaC, demo-infra, evidence and Esri
compatibility. The existing contexts in the other repositories still need the
implementation qualification described below. No collaborator was added and no
repository setting was changed. The lane requested an authorized additional
human owner; it cannot invent one or approve Mike-authored work as Mike.

Evidence files use UTF-8/LF on Windows and in Git. Capture and audit explicitly
write LF, and `.gitattributes` preserves the policy/snapshot bytes across
checkouts so their SHA-256 bindings remain reproducible. The native Windows
focused suite covers both capture and failure-receipt serialization, alongside
the independently specified control fixtures. An observed failure is not a
released pre-cut criterion; only the actual-candidate signature receipt must
wait for its tag.

Native validation: **44 focused tests pass**; the failure receipt's policy and
snapshot hashes also match the committed Git blobs. The broader Windows trial
reports **825 passed, 10 failed**: four Bash-dependent workflow assertions,
four copied pytest-cache access failures, and two default Windows text-encoding
failures. The broad run exposed subprocess Bash invocations in existing tests;
it is not rerun locally under the Windows-only host rule. This is not a green
full-suite claim. Required hosted `validate` must pass at the PR head; no test is
weakened, skipped or deleted to change these results.

## Concrete proposal

`policy.json` enumerates all 17 repositories from the adopted issue inventory,
including all nine manifest components. The renderer preserves the currently
required exact contexts as a baseline, binds them to the GitHub Actions app
(15368), and adds strict release-line checks, one code-owner review, stale-review
dismissal, last-push approval, resolved threads, deletion/force-push prevention,
and **no bypass actors**. It targets `refs/heads/release/*`; it neither creates a
maintenance branch nor changes default-branch policy. In particular, server
trunk's adopted `strict=false` exception remains intact. SDK native `release/x.y`
and platform `release/2026.1` branches both match this pattern. Existing scratch
branches under `release/` also match and must be considered during rollout.
Site/demo/evidence may remain trunk-based and bind immutable snapshots to the
platform lock; future maintenance refs still receive the proposed protection.

The four empty denominators (IaC, demo-infra, evidence, Esri compatibility) are
intentional recorded failures. Rendering them raises an error; no empty-check
ruleset can be installed by copying the output. Existing required contexts are
**not** automatically evidence that their aggregate implementations fail closed.
That implementation audit and missing aggregate work remain open under #236.

Render the exact reviewable API payloads without changing GitHub:

```sh
python3 tools/release_controls.py render honua-release > /tmp/release-branch-rules.json
python3 tools/release_controls.py render honua-release --tags > /tmp/release-tag-rules.json
```

Once the human ownership prerequisite and check qualification are satisfied,
apply these as repository rulesets through GitHub's rules API. Read back the
result and compare it with the expected payload. Do not delete or weaken existing
rulesets: GitHub aggregates matching rulesets, and the additional no-bypass rule
must stand on its own. No admin:org scope is needed for repository rulesets.
The current token's organization rules endpoint returns HTTP 404 with an explicit
admin:org scope diagnostic, but its repository permissions include admin.

## Evidence and repeatable validation

```sh
python3 tools/release_controls.py capture --output /tmp/release-controls-live.json
python3 tools/release_controls.py audit /tmp/release-controls-live.json --output /tmp/release-controls-result.json
python3 -m pytest tools/test_release_controls.py -q
```

Capture uses only GET requests, paginates the ruleset/collaborator denominator,
and reads ownership at the inspected source SHA. Transient failures and 403s cool
down and retry the same command (10/30/60/120/60 seconds); it never authenticates.
Audit requires the complete repository denominator, records missing/unreadable
controls as failures, and writes SHA-256 bindings to the policy and snapshot.
A hash binds these committed bytes; it is not a signature or proof of freshness.
The committed snapshot is a point-in-time audit, not ongoing enforcement.

The committed `audit-2026-09-06.json` is deliberately **fail**. Reproducing that
failure is expected; do not wire this historical receipt into `validate` as if it
were a current live gate. The PR's existing `validate` job runs the regression
suite, which independently specifies the required API values and mutates each
security control to prove rejection. It also rejects combining a bypassable
checks rule with a separate review rule into a fictitious complete protection.

## Signed publication tags — producer and trust policy

`certification/release-controls/tag-signing-policy.json` is the trust policy, and
`tools/tag_signing.py` is the producer and verifier. Together they close the "implement a
signing producer with an explicit trust policy" item below; the item's remaining half, an
authorized signing key, is an operator input this repository must not invent.

Three things are routinely mistaken for a signed tag, and all three are refused:

- GitHub's ruleset `required_signatures` rule verifies **commit** signatures, not tag objects.
- The renderer's tag rules (`update`, `deletion`) give **immutability**. Immutability is not a
  signature.
- `gh release create`, which the current promotion workflow uses, writes a **lightweight** tag:
  a ref straight at a commit, with no tag object, so there is nothing that could be signed. The
  platform promotion must move to this producer before the candidate is cut.

A qualified publication tag is an annotated tag object whose signature verifies against a signer
the trust policy names by fingerprint. The policy holds **fingerprints only**; public key
material stays in an operator-supplied allowed-signers file that is never committed, and any
principal in that file whose fingerprint the policy does not name is rejected — widening the
operator's trust material cannot widen the policy.

```sh
python3 tools/tag_signing.py check-policy
python3 tools/tag_signing.py sign honua-release honua-2026.1.0-rc.1 \
  --target <40-hex commit> --message "2026.1.0-rc.1" --git-dir . \
  --allowed-signers /path/to/allowed_signers --output signed-tag-receipt.json
python3 tools/tag_signing.py verify honua-release honua-2026.1.0-rc.1 \
  --git-dir . --allowed-signers /path/to/allowed_signers
```

`sign` refuses a tag outside the repository's protected namespace, a mutable target, an existing
tag (publication tags are never moved), and a configured key the policy does not authorize; it
then verifies what it actually wrote. `audit` accepts `--signing-receipts <file>`, a mapping of
repository to receipt, and drops the signed-tag failure **only** for a receipt bound to the
committed trust policy's exact bytes, inside the protected namespace, naming a real annotated
tag object and an authorized fingerprint.

The committed policy nominates **no signer**, so every command above fails closed today and the
audit stays red for all five repositories with native publication tags (`geospatial-grpc`,
`honua-helm`, `honua-release`, `honua-sdk-dotnet`, `honua-sdk-python`). The trust policy's tag
namespaces are checked against `policy.json`, so the two cannot drift apart.

Regression evidence lives in `tools/test_tag_signing.py`. It does not mock signatures: each test
builds a throwaway repository, generates an ephemeral SSH key inside the test's temporary
directory, and makes git produce a real signed tag object. Fingerprints are computed
independently with `ssh-keygen -lf`, and the tamper test rewrites signed tag bytes and rehashes
the object so a genuinely broken signature is what fails. Adding `--signing-receipts` did not
change any committed audit: `audit-2026-09-06.json` and `audit-2026-09-06-windows.json` both
still reproduce byte-for-byte from their snapshots.

## Unmet acceptance criteria and sequencing

- Nominate and authorize an independent human code owner, add that identity to
  the policy and CODEOWNERS, and land ownership PRs in the remaining repositories.
  No user/team is invented and no collaborator access is granted by this PR.
- Qualify each aggregate implementation and implement the four missing required
  check denominators before activating their rulesets.
- Apply/read back all release-line controls and prove a denied unreviewed change
  plus an independently reviewed allowed change. Those are pre-cut requirements;
  the absence of a candidate does not release them.
- Nominate an authorized publication signing key and record its fingerprint in
  `tag-signing-policy.json`. The producer, trust policy and verifier now exist
  (see above), but the policy names no signer, so no publication tag can be
  produced or trusted and `audit_repository` still reports every tag-publishing
  repository as unqualified. It accepts a `tag_signing` receipt bound to the
  committed policy bytes, never a snapshot boolean.
- Qualify the native tag namespace in repositories without declared tag rules, and
  enforce tag immutability. The renderer's tag rules prevent update/deletion; they
  do **not** require annotated-tag signatures.
- Move the platform promotion off `gh release create` for the publication tag. It
  signs release blobs with Sigstore but writes a lightweight Git tag, which is not
  signed-tag evidence.
- The signature receipt for the actual candidate/native release tags must wait
  until those tags exist. That receipt alone is candidate-dependent. The signing
  producer, trust policy, and branch controls remain pre-cut implementation work.

GitHub semantics: [ruleset API](https://docs.github.com/en/rest/repos/rules),
[available rules](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets),
[CODEOWNERS](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners).
