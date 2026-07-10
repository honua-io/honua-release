# GitHub Actions pin review

Every non-local `uses:` declaration in this repository is executable release-chain code. It must be
pinned to a full 40-character commit SHA (or an immutable `sha256` digest for a Docker action) and
retain the corresponding human-readable version in an inline comment.

`tools/check_action_pins.py` scans both reusable workflows and composite actions. It has no
third-party dependency and runs near the start of the branch-protected `validate` job. A tag,
branch, shortened SHA, unpinned Docker image, or missing version comment fails that job. Local
`./.github/...` actions and reusable workflows are repository content reviewed in the same PR and
are exempt.

## Updating a pin

Dependabot opens reviewed GitHub Actions update PRs weekly. It does not have merge authority. For
every pin change, the reviewer must:

1. Read the upstream release notes and changelog from the currently pinned version through the
   proposed version. Pay particular attention to permissions, runtime changes, network access,
   artifact retention, credentials, and publishing behavior.
2. Confirm that the repository owner and action path are unchanged. Treat an owner transfer,
   archived repository, new marketplace publisher, or unexpected action-path change as a security
   review, not a routine dependency bump.
3. Resolve the exact release tag through GitHub and verify that it produces the proposed SHA:

   ```bash
   gh api repos/OWNER/REPOSITORY/commits/vX.Y.Z \
     --jq '{sha, verified: .commit.verification.verified, reason: .commit.verification.reason}'
   ```

   The returned `sha` must equal the workflow pin. Record the command output in the PR. A verified
   signature is preferred. If the upstream commit is unsigned, the reviewer must say so explicitly
   and corroborate the SHA against the upstream release/tag before accepting it.
4. Preserve or update the exact version comment beside every changed SHA and run:

   ```bash
   python3 tools/check_action_pins.py
   python -m pytest tools/test_action_pins.py -q
   ```

5. Require the protected `validate` check and every workflow touched by the update to pass. Never
   auto-merge an action update, and never weaken release candidate, environment, or promotion gates
   to make a dependency bump green.

For Docker actions, review the image release notes and verify its registry digest using the
publisher's signed provenance when available. The workflow must use `@sha256:<64 hex characters>`;
an image tag may appear only in the adjacent human-readable comment.
