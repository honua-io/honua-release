# Exact GP devkit publication

`gp-devkit-publish.yml` consumes the retained successful candidate run and its exact artifact IDs. It does not rebuild the image, repack the tool, or advance a platform manifest. Candidate source commits must already belong to trusted trunk.

The workflow uses the existing `RELEASE_GH_TOKEN` for public honua-server release assets and its own package-scoped `GITHUB_TOKEN` for GHCR. The publishing repository must have write access to `honua-gp-workspace`. No credentials belong in the package, image or browser.

An interrupted publication can resume only when the existing image config/platform and release target/assets match the selected qualified bytes. Missing assets can be uploaded; conflicting tags, foreign assets, altered bytes and unverifiable permission states stop the workflow. Uploads never clobber existing assets. An existing release without a verifiable matching registry image is an inconsistent state requiring investigation; it cannot authorize another push. Unknown registry, credential-helper, and permission failures are never interpreted as a missing image.

GitHub Container Registry creates new packages privately by default. Initial package public visibility and repository access must be configured and independently verified before this workflow can pass anonymous consumption. A successful authenticated push is not proof of public availability. The fresh anonymous job uses an empty Docker configuration and no registry login; failure leaves public consumption unqualified.

The installed GitHub CLI verification output was checked against CLI v2.98.0's documented `verificationResult.statement.predicate` shape and its signed upstream SLSA v1 fixture. The invocation binding is the exact Actions run URL including `/attempts/N`. This schema inspection does not replace cryptographic verification of the actual selected Honua candidate during publication.

Publication and anonymous consumption have not yet run against an actual qualified public candidate. Local tests verify rejection and reconciliation boundaries only.
