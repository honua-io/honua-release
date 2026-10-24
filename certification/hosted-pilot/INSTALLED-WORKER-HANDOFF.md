# Installed worker handoff

Installation has four separately observed stages. None of the first three is an
execution qualification or a human approval.

1. `prepare-installed-mcp-base` authenticates the original native-bootstrap
   completion and issued task-key record, observes that immutable key's target
   and effective permissions, then writes one immutable MCP base. The task key
   must carry exactly this runtime's `agent:runtime-task:<runtimeId>` marker and
   the reviewed mutation ceilings. The result remains `registration-pending`,
   with `activation:false` and `qualification:false`. It contains no workspace,
   turn, execution capability, control credential, or per-turn secret update.
2. A separately funded checked worker plan/apply creates the fixed slot. After
   native readiness succeeds, the apply workflow retains an identity-only
   installation object in the original accounting bucket. The public artifact
   contains only its immutable `{bucket,key,versionId,sha256}` reference.
3. A dedicated privileged registrar consumes that original installation and a
   reviewed controller declaration. It must wait until the producing worker
   workflow has completed successfully. The long-lived controller does not
   receive the registrar's original application administrator credential.
4. Controller activation additionally requires the actual native registration
   GET receipt and heartbeat. A pending MCP base or successful Terraform apply
   cannot substitute for either observation.

`tools/hosted_pilot/installed_worker_receipt.py::verify_installation` accepts the
immutable installation reference plus `durable`, `runtime_id`, `target_id`,
`original_expires_epoch`, `s3`, `ecs`, `states`, and a fenced `guard`. It verifies
the original successful workflow jobs, protected source/configuration, saved-plan
review, readiness objects, pending MCP originals, and current fixed resources
through the maintained `InstalledSlot.inspect` implementation. Its return contains
the descriptor, three original state references (`mcpBootstrap`,
`issuedAgentKey`, `nativeBootstrap`), producer/source/plan metadata, and proof.
The return explicitly retains `activation:false` and `qualification:false`.
`proof.nativeEndpoint` is the original native origin; aliases are not inferred.

The pending writer and installation verifier require the original authenticated
interactive funding deadline. Provisioning and registration need their own
retained infrastructure and cleanup reservations with actual reviewed positive
bounds. Registration uses a distinct bootstrap operation; it cannot reuse native
bootstrap or controller reservations. Nothing in this handoff creates an
allowance, renews expiry, settles unknown costs, or clears an occupied slot.

The registrar source entry is fixed to
`python tools/hosted_pilot/native_runtime_registration.py`, running as the
dedicated nonroot `runtime-registration` container. It verifies its actual task,
immutable image and three version-pinned credential injections before using the
checked declaration loader and installed-worker verifier. The registration
protocol retains its original POST intent and response; recovery performs GET
readback without repeating the registration POST or issuing replacement keys.

`registration_allowance.verify_retained_admission` is a read-only prerequisite
to every registrar effect. The separately authenticated producer must first
retain the original admission in append-only candidate custody, then reserve
distinct infrastructure and cleanup rows against the reviewed quote. Its
absolute deadline is the earlier of the original funding expiry and the original
admission time plus the quoted lifetime (at most 600 seconds). A process restart,
replacement task or missing response cannot create a new deadline. The task
must use the quoted 256 CPU units, 1024 MiB memory and default ephemeral storage.

The optional runtime Terraform input creates a separate control credential and
pins the original root credential for the bootstrap/registrar path. The ordinary
controller receives the exact control credential version and never the root
credential. The input is absent by default and does not activate execution.

The registrar task definition, checked preparation/dispatch route, independent
same-deadline reaper, actual positive quote and deployed references remain
required integration steps. This source interface does not assert a deployed
registration, successful execution, cleanup or settlement.
