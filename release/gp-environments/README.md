# GP process environments

One directory per environment. `environment.json` (schema `gp-environment-state.v1`) declares the
server execution-contract version, the executable target kinds, the last observed binding version
and the deployment tooling kind. `<process>.pin.json` (schema `gp-environment-pin.v1`) binds a
logical process name to one release lock by path and digest and records the previous release as
the rollback target. `gp-promote-process.yml` writes pins; a merged pin change on `trunk` starts
`gp-deploy-process.yml`. See `docs/GP-PROCESS-RELEASES.md`.

`dev/` is a reference environment (`deploymentTooling.kind: reference`): it exercises the whole
path without a cluster and never serves anything.
