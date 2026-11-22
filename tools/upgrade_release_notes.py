"""Shared forward-upgrade policy for candidate and final release notes."""

UPGRADE_POLICY = (
    "The supported qualification source for `2026.1.0` GA is the published "
    "`2026.1.0-rc.2` quickstart, pinned by its finalized manifest and immutable server digest. "
    "Legacy `2026.1-rc.2` tags, with optional `honua-` prefixes, identify the same baseline; "
    "nightly images and other RCs cannot substitute. This source declaration does not certify "
    "an upgrade: publication requires a passing installed, candidate-bound forward-upgrade receipt "
    "that preserves data, published services, saved maps, stored credentials and existing API keys. "
    "The GA server runtime and OCI version must both report `2026.1.0`. "
    "Migrations are forward-only and must be idempotent and tested on the installed stack. "
    "There is no rolling rollback or N-1 database-reader compatibility contract. "
    "Back up before upgrading; do not run an older server against the migrated database. "
    "Recovery from a failed upgrade requires a reviewed fix forward or a separately restored "
    "pre-upgrade backup with its matching immutable artifacts. "
    "The qualification procedure is `docs/GA-UPGRADE-QUALIFICATION.md`."
)
