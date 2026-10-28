# G3 clean-machine quickstart run — 2026-10-28

Launch gate for honua-release#1009. The public Docker Compose quickstart was followed as written, except the `HONUA_IMAGE` line. This dispatch set it to `ghcr.io/honua-io/honua-server:nightly-a4bde4b` instead of the page's `ghcr.io/honua-io/honua-server:2026.1-rc`.

## Result

**Passed for `ghcr.io/honua-io/honua-server:nightly-a4bde4b` only.** The documented fences ran with that one `HONUA_IMAGE` change, and the query printed the two `places` features. This record does not turn G3 green for the documented channel tag. The first-map page was not part of this dispatch.

## Host

| Field | Value |
| --- | --- |
| Date | 2026-10-28, 17:30:35Z to 17:31:10Z |
| OS | Linux 7.0.0-1017-azure |
| Docker | 28.0.4 |
| Compose | Docker Compose version v2.38.2 |
| Python | Python 3.12.3 |
| OpenSSL | OpenSSL 3.0.13 30 Jan 2024 (Library: OpenSSL 3.0.13 30 Jan 2024) |
| Run directory | A new empty directory, not a git checkout |
| Machine | GitHub-hosted ubuntu-latest, x86_64. Run 40736340734, attempt 1. The quickstart directory was created empty and was not a git checkout. honua-release was checked out in another directory so this receipt could be committed. |
| Quickstart | https://github.com/honua-io/honua-server/blob/d04af1cc39408545ae815589ae9b4c7c7ec1ff57/docs/get-started/quickstart.md |
| Image | ghcr.io/honua-io/honua-server:nightly-a4bde4b |
| Documented image | ghcr.io/honua-io/honua-server:2026.1-rc |

## Steps

Source: https://github.com/honua-io/honua-server/blob/d04af1cc39408545ae815589ae9b4c7c7ec1ff57/docs/get-started/quickstart.md. Bash fences were copied unchanged into one shell and executed with `bash --noprofile --norc` and `set -e`, so a failed command stops the step and the virtualenv activation persists. `mode=skip` fences were not run. After those fences, the harness runs `docker compose down --volumes` when `compose.yaml` exists. The secrets fence differs by the `HONUA_IMAGE` line only.

| Step | UTC | Duration | Outcome |
| --- | --- | --- | --- |
| `install.compose` | 17:30:35Z | under 1s | Wrote `compose.yaml`. `platform: linux/amd64` is in the documented service. |
| `install.secrets` | 17:30:35Z | under 1s | Exit 0. `.env` created. Secrets are not in this record. |
| `install.windows-secrets` | — | — | Skipped. Native Windows alternative. |
| `install.keyring` | 17:30:35Z | under 1s | Exit 0. `secrets/keyring.pfx` created. |
| `install.start` | 17:30:35Z–17:31:03Z | 28s | Exit 0. |
| `install.seed` | 17:31:03Z–17:31:04Z | 1s | Exit 0. |
| `install.clients` | 17:31:04Z–17:31:09Z | 5s | Exit 0. |
| `install.publish` | 17:31:09Z | under 1s | Wrote `quickstart.py`. |
| `install.query` | 17:31:09Z | under 1s | Exit 0. |
| `install.cleanup` | — | — | Skipped. Harness tears down its isolated project on every exit. |

## Record

Local image digest: `ghcr.io/honua-io/honua-server@sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37`.

This run did not substitute the pre-cut rehearsal digest in `customer-install-manifest.json`.

Harness teardown: exit 0.
