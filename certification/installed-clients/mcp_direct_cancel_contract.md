# Original direct-cancel source control

The protected installed-client producer submits one fresh, bounded managed
buffer job over 10,000 MultiPoint coordinates through each canonical client.
Its complete `plan` argument stays below 120 KiB so Inspector's CLI can pass it
as one process argument; the server still buffers every coordinate.
It must observe that exact job
`Running` before requesting `honua_cancel_job` with the original managed
owner key. An acknowledgement is only an intermediate observation: the
producer also requires durable `Cancelled`, an empty result package, and a
second job/result/store read after three seconds with no late output. The
read-only managed key and anonymous caller must receive typed denials for
the same job. The read-only denial is sent in that job's tenant; a
cross-tenant `not_found` does not prove the key lacks cancel permission.
The owner may repeat the cancellation idempotently.

The exact owned Docker tenant options are read back before any call. An admin
key selects a fresh tenant on both fixed-client HTTP requests, then the same
key selects the declared public tenant and must be denied on the original job.
This proves the server's tenant boundary for this admin path; it does not
claim non-admin owner isolation. The independently captured Redis job record
binds the full authored plan, canonical submitting actor, selected tenant,
exact job ID, and empty artifact/effect references. Its
atomic job/result census and source/image readback must remain stable. The
managed-key helper creates and revokes only owned disposable keys; private
key files remain local and are excluded from the public proof.

The controls in this repository exercise the original wire, factory,
managed-key lifecycle, Redis projection, and adversarial refusals using a
controlled offline process. They do not execute a Honua image or prove
native child-process cessation. Candidate credit requires the selected
immutable image, exact source and client versions, actual managed identities,
and the full live journey. A job that completes before cancellation is a
nonpass, even if the tool acknowledged a request.
