# Clean-room installation and failure rehearsal — 2026-09-04

**pre-cut, current packages** — in progress; this is not exact-candidate certification.

Native Windows host, PowerShell, Docker Desktop. Rehearsal directory:
`C:\Users\mike\honua-io\clean-room-20260904`, verified outside Git with
`git rev-parse --show-toplevel` (not a repository). No WSL or Bash invoked.

## Initial evidence

- Started approximately 10:57 HST (UTC−10); timings below use HST.
- Record base: `7870615`; site base: `06d3ab8d8555b0888841072eafba334dabbe8566`.
- Both working branches pushed immediately after creation, approximately 11:01.
- Docker client/server `29.6.2`; Compose `v5.3.1`.
- Supplied two synthetic public-domain GeoJSON points, names Rehearsal A/B,
  coordinates `[-157.8583,21.3069]` and `[-157.85,21.30]`.
  File SHA-256: `8579cfd11e6f7725e6b82e54d36955aeb2444fd097210e3b95649face7477c39`.
- 11:03: anonymous manifest lookup of the public image named at
  <https://honua.io/docs.html#pin> succeeded using a fresh empty Docker config.
  Linux/amd64 package digest:
  `ghcr.io/honua-io/honua-server@sha256:e971442db410dc0e095a9073d70195d0841d196a303050bdb7efa189195109fc`.
- 11:05: pull succeeded. Exact inline Compose content from
  <https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps>
  copied without reading server source; SHA-256
  `e41145376cb02164654d60f379c058b31d282ea75886b2c9ffc27747dfcdf1f2`.
- 11:05:40: `docker --config ./docker-config compose --project-name honua-rehearsal-20260904 config --quiet`
  passed. Disposable credentials generated outside Git in the documented `.env`,
  with an owner-only Windows file ACL; values are not included in this record.

## Findings so far

| ID | Exact documentation section | Observation / intervention | Status |
| --- | --- | --- | --- |
| F01 | [Site fast lane](https://honua.io/docs.html#quickstart) | Omits GitHub CLI/package-read access, Python, and the explicit source-image build required by the linked quickstart; advertises GeoJSON import while quickstart seeds SQL. | Site documentation correction pending |
| F02 | [Quickstart, Steps 1–2 and 6](https://honua.gitbook.io/honuaio/get-started/quickstart#steps) | Requires Bash build script and source-installed Python SDK/admin packages. One-terminal setup also invokes a developer validation script. Cannot execute as a package-only Windows customer installation. | Server issue pending |
| F03 | [Docker Compose, Steps 1–3](https://honua.gitbook.io/honuaio/guides/deploy-and-operate/docker-compose#steps) | Shell heredocs, `/opt/honua`, `chmod`, and systemd/Caddy commands have no PowerShell equivalents. Used fresh Windows directory, literal file writes and owner ACL. Bound local evaluation only; no DNS/TLS proxy created. Replaced release-digest placeholder with resolved current public package digest. | Deviations; no claim of production/TLS verification |

## Remaining walkthrough

Start package stack → import supplied file → publish → supported-client query →
restart → documented diagnostics → recovery → teardown. Results and issue/PR links
will be recorded here before completion. Repeat the entire walk against the exact
candidate after the cut; do not carry this pre-cut evidence forward as a pass.
