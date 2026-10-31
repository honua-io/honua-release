# Studio customer journey RC gate

`gate-studio-customer-journey.yml` is a required candidate-bound RC admission row in
`tools/rc_gate_window.py`. It runs Docker Compose in Console CI; it creates no AWS resources
and does not dispatch any release cloud workflow. Run it on reviewed release trunk with the
candidate's actual `platform-lock.json` SHA-256 in `platform_lock_digest`. An immutable
`candidate_ref` can adopt the candidate manifest through the existing verified snapshot action.

The gate reads both `honua-console` and `honua-server` source SHAs and GHCR digests from that
manifest. It dispatches `console-live-journey.yml` at the exact Console SHA with
`suite=studio-customer`, `console_image`, `server_image` and a unique correlation ID.
`RELEASE_GH_TOKEN` needs Actions write on Console to dispatch and Actions read to retrieve
that run's evidence. No source ref or image override can replace the manifest pins.

The wrapper waits for that exact dispatch, requires success with no failed, flaky or skipped
tests, and validates the runtime image digests and source labels plus the complete customer
progress attachment. It writes a report binding the server SHA, server digest, platform lock,
manifest digest, Console SHA, Console digest and Console run URL. The newest failed run for
the same candidate supersedes any older passing run. Missing evidence fails admission.

The customer suite covers a fresh install-publisher OIDC session, short-lived bearer refresh, two
file imports and publications, typed OGC/FeatureServer fields, two map layers, category style,
attribute labels, filtering, table transport, saved version ownership, reload/reopen,
independent server export pixels and negative controls, public audience and axe checks.
A publisher's self-approval attempt is denied; a different operator approves publication.
A permission denial or missing authoring/render capability stays red; it cannot be recast as
an infrastructure success. The customer authenticates as the install publisher; the
testbed verifier's API key is never given to the browser or Console.

The release session must update its own candidate files after the Console suite and required
product repairs land: pin the new Console source SHA and published digest, retain the selected
server digest, freeze a matching platform lock, and dispatch this gate with that lock digest.
Record the resulting report/run in the release evidence and notes. This lane does not edit
`platform-manifest.yaml`, release notes, `e2e/`, terminal certification, or cloud workflows.
Old Console pins that do not contain this workflow input contract fail at dispatch.
