# G3 clean-machine quickstart run — 2026-10-30

Launch gate for honua-release#1009. The public Docker Compose quickstart was followed as written.

## Result

**Passed.** The public Compose quickstart finished on this runner. The documented fences ran, and the query printed the two `places` features (`west` and `east`). This record is the G3 evidence for that procedure on `ghcr.io/honua-io/honua-server:2026.1-rc`. The first-map page was not part of this dispatch.

## Host

| Field | Value |
| --- | --- |
| Date | 2026-10-30, 11:57:45Z to 11:58:14Z |
| OS | Linux 7.0.0-1017-azure |
| Docker | 28.0.4 |
| Compose | Docker Compose version v2.38.2 |
| Python | Python 3.12.3 |
| OpenSSL | OpenSSL 3.0.13 30 Jan 2024 (Library: OpenSSL 3.0.13 30 Jan 2024) |
| Run directory | A new empty directory, not a git checkout |
| Machine | GitHub-hosted ubuntu-latest, x86_64. Run 41051799348, attempt 1. The quickstart directory was created empty and was not a git checkout. honua-release was checked out in another directory so this receipt could be committed. |
| Quickstart | https://github.com/honua-io/honua-server/blob/7a6eec9bf471243400c19e2ca75e72c450e004b5/docs/get-started/quickstart.md |
| Image | ghcr.io/honua-io/honua-server:2026.1-rc |

## Steps

Source: https://github.com/honua-io/honua-server/blob/7a6eec9bf471243400c19e2ca75e72c450e004b5/docs/get-started/quickstart.md. Bash fences were copied unchanged into one shell and executed with `bash --noprofile --norc` and `set -e`, so a failed command stops the step and the virtualenv activation persists. `mode=skip` fences were not run. After those fences, the harness runs `docker compose down --volumes` when `compose.yaml` exists.

| Step | UTC | Duration | Outcome |
| --- | --- | --- | --- |
| `install.compose` | 11:57:45Z | under 1s | Wrote `compose.yaml`. `platform: linux/amd64` is in the documented service. |
| `install.secrets` | 11:57:45Z–11:57:46Z | 1s | Exit 0. `.env` created. Secrets are not in this record. |
| `install.windows-secrets` | — | — | Skipped. Native Windows alternative. |
| `install.keyring` | 11:57:46Z | under 1s | Exit 0. `secrets/keyring.pfx` created. |
| `install.start` | 11:57:46Z–11:58:08Z | 22s | Exit 0. |
| `install.seed` | 11:58:08Z | under 1s | Exit 0. |
| `install.clients` | 11:58:08Z–11:58:13Z | 5s | Exit 0. |
| `install.publish` | 11:58:13Z | under 1s | Wrote `quickstart.py`. |
| `install.query` | 11:58:13Z–11:58:14Z | 1s | Exit 0. |
| `install.cleanup` | — | — | Skipped. Harness tears down its isolated project on every exit. |

## Record

Local image digest: `ghcr.io/honua-io/honua-server@sha256:c8d7aef98761618145725530b6a4e4b25ea4a9ae121da168362f9be4cf5f7d37`.

This run did not substitute the pre-cut rehearsal digest in `customer-install-manifest.json`.

Harness teardown: exit 0.
