# G3 clean-machine quickstart run — 2026-10-27

Launch gate for honua-release#1009. The public Docker Compose quickstart was
followed as written. The run stopped before the first map.

## Result

**Failed.** `docker compose up` could not pull the image the quickstart sets:

`ghcr.io/honua-io/honua-server:2026.1-rc: not found`

No service was created. Publish, query, and the first map were not reached.
This record does not turn G3 green.

## Host

| Field | Value |
| --- | --- |
| Date | 2026-10-27, 02:22:06Z to 02:22:09Z |
| OS | Linux 6.18.54.1-microsoft-standard-WSL2, x86_64 |
| Docker | 29.8.0, Compose with `--wait --wait-timeout 180` |
| Run directory | A new directory with no git checkout |
| Machine | This dev workstation. Other Honua Compose projects were already running. `virt-install`, `multipass`, `incus`, and `qemu-system-x86_64` were not on `PATH`, so this was not a separate empty VM. |

## Steps

Source: `honua-server` `docs/get-started/quickstart.md` on `origin/trunk`. The
compose file in the run directory was that page's step 1 YAML. Step 2 wrote
`.env` with `HONUA_IMAGE=ghcr.io/honua-io/honua-server:2026.1-rc` and
`HONUA_HTTP_PORT=18080`, then the documented OpenSSL key ring. Ports 18080 and
18081 were free.

| Step | UTC | Duration | Outcome |
| --- | --- | --- | --- |
| 1. Write `compose.yaml` | 02:22:06Z | under 1s | File written. `platform: linux/amd64` is in the documented service. |
| 2. Secrets and key ring | 02:22:07Z–02:22:08Z | about 1s | `.env` and `secrets/keyring.pfx` created. Secrets are not in this record. |
| 3. `docker compose up -d --wait --wait-timeout 180` | 02:22:08Z–02:22:09Z | about 1s | Exit 1. Image pull failed. `docker compose ps` was empty. |

Daemon error:

```
Image ghcr.io/honua-io/honua-server:2026.1-rc Pulling
Image ghcr.io/honua-io/honua-server:2026.1-rc Error failed to resolve reference "ghcr.io/honua-io/honua-server:2026.1-rc": ghcr.io/honua-io/honua-server:2026.1-rc: not found
Error response from daemon: failed to resolve reference "ghcr.io/honua-io/honua-server:2026.1-rc": ghcr.io/honua-io/honua-server:2026.1-rc: not found
```

An anonymous GHCR read of the tag returned HTTP 404 `MANIFEST_UNKNOWN`. The
same read of digest `sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a`
returned HTTP 200 and includes `linux/amd64`. That digest is
`server.image` in `customer-install-manifest.json`, whose `status` is
`pre-cut-rehearsal`. It is not the `2026.1-rc` channel tag, and this run did
not substitute it.

## Defect

The quickstart names the right channel tag. The tag is not published. No
documentation edit was opened. Publishing `2026.1-rc` belongs with minting the
Release Candidate, which is still open.
