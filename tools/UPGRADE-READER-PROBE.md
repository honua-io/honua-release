# Launch Stack reader assertions

`upgrade_reader_probe.probe_reader(ReaderProbe(...))` supplies the server-side
read/edit assertions for `operates-8-upgrade`. It reuses the managed scratch
edit proof and canonical `public.schema_versions.scriptname` journal digest.
Run from the repository root with the root and `tools/` on `PYTHONPATH`.

The deployment adapter supplies:

- `publication`: the actual managed `maui-inspections` scratch publication returned
  by the seed operation, including its positive `layerId`.
- `candidate_journal_digest`: `components.honua-server.migrationJournalSha256` from the
  authenticated candidate platform lock, never a digest of whatever happens to
  be installed.
- `request(path, values, method="GET")`: an authenticated JSON-response transport
  pinned to the task under test. GET values are query parameters; POST values
  are form encoded. Reject redirects and HTTP/API errors, bound response sizes
  and timeouts, and keep credentials out of observations.
- `read_journal()`: query the **same Launch Stack database** directly and return
  its actual journal script names as a list. Do not provide a retained fixture
  or reported hash. Database identity and credentials belong to the adapter.

The probe requires the candidate journal before touching the scratch feature,
changes its note to a unique value, reads back attributes and geometry, then
restores and reads back the original feature even when the mutation check fails.
It checks the journal again even if the reader is unavailable. A startup refusal,
HTTP-200 edit failure, unpersisted edit, failed restoration or changed journal
raises an exception. An expected N-1 startup refusal is not reader compatibility.
Use an exclusive scratch publication; concurrent edits invalidate this proof.

Invoke after candidate migrations while the previous-release task is still
serving, and again after rollback to the previous release. Route directly to the
selected task (or an exclusive target group); a load balancer alternating prior
and candidate tasks cannot identify an N-1 reader. Snapshot the journal after
reader startup too: this probe cannot detect a mutation already repaired before
its first database query. Retain observed task/image identities, phase ordering,
database binding, timestamps, rolling replacement evidence and bounded recovery
time separately in the shared gate adapter.

The returned journal hash and scratch-edit observations are **not** a release
receipt. They do not establish `live`, `launchStack`, `rollingUpgrade`, or task
identity. Only the protected authenticated Actions producer, using the shared
lease/binding harness and real deployment observations, can assemble the
candidate-bound receipt. The existing Compose/kind upgrade jobs do not qualify.
