# Issue 377 local Docker observation — 2026-09-29

- Receipt SHA-256: `d70981e174b5ac33cde5c65377afda637dd396dc4993f26ea80955645d719c22`
- Schema migration (honua-release#491): added `operationInstanceId`, `correlationId`, `auditId` and `proposalId` as `null` to every stage row, which the receipt schema now requires on all rows. Nothing observed was changed. As-recorded SHA-256: `091da311104f69e4edcedd491598529182b009121d60fda8b29413d3f5489982`
- Status: `fail`; no candidate qualification.
- Candidate snapshot: `2026.1-rc.2`, source `87966c3f7b6c840ffc4d4da0b451714ab717b18a`.
- Image: `ghcr.io/honua-io/honua-server:nightly-87966c3@sha256:069f196bfa5c7201223d4d89868934242c4ace8805a6e48c122a88d84fa6eb1a`.
- Observation time: `2026-09-29T17:55:23.358376Z`.

The release promise at issue is the terminal journey from an installed candidate
through build, update, rollback, and operate, including separate-principal approval.
This is an actual local Docker run using the unchanged manifest image and published
client tarballs verified against their frozen integrity pins. It does not close #377.

```sh
NPM_CONFIG_CACHE=/tmp/release-377-npm-cache python3 certification/terminal-journey/run.py \
  --mode live --target certification/terminal-journey/targets/local-docker.json \
  --workdir /tmp/release-377-live-retry \
  --output /tmp/release-377-live-retry/receipt.json \
  --evidence-uri https://github.com/honua-io/honua-release/issues/377
```

The image became ready, reported the exact source revision, ran with licensing
disabled, and refused anonymous admin access with HTTP 401. Direct HTTP setup
discovery passed the #366 measurements. Installed-proxy parity failed. Independently
launching the pinned `honua-mcp-proxy` installed executable with an initialize request
and `HONUA_MCP_REMOTE_URL` returned exit 0 with zero stdout and stderr bytes. No
resolved-module or locally built client substituted for that failed installed path.

Stage 2 failed at `createAdminApiKey`: exit 2, no private-sink receipt. The printed
failure identifies `credential-preflight` and its command. Stages 3–8 remain blocked
by their existing executable-driver requirements. This receipt contains no new
proof of self-approval denial, prompt-injection handling, incompatible-target
preflight, exactly-once GP completion, or schema rollback behavior.

The first attempt could not install npm dependencies using the lane's default cache.
The recorded rerun used a writable temporary cache and completed installation of the
same verified tarballs. Docker teardown completed; no stack was retained. Discovery
sidecars and client installation directories stay local and are not included as
public receipt payloads.
