# Original direct-cancel source control

The protected installed-client producer submits one fresh, bounded managed
buffer job through each canonical client. It must observe that exact job
`Running` before requesting `honua_cancel_job` with the original managed
owner key. An acknowledgement is only an intermediate observation: the
producer also requires durable `Cancelled`, an empty result package, and a
second job/result/store read after three seconds with no late output. The
read-only managed key and anonymous caller must receive typed denials for
the same job; the owner may repeat the cancellation idempotently.

The independently captured Redis job record binds the full authored plan,
the canonical owner, exact job ID, and empty artifact/effect references. Its
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
