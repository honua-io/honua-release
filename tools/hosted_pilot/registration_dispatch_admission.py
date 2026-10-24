"""Authenticate an original registration admission without acquiring a lease.

The dispatcher must condition-fence the returned actual head in its permanent
launch claim transaction. This reader never fabricates a live lease and cannot
create an admission, reserve money or initialize accounting.
"""
from types import SimpleNamespace

from accounting_snapshot import observe
from registration_allowance import _verify_retained_admission


def verify_original_dispatch_admission(config, store, *, source_reader, clock):
    # Both immutable objects and the unchanged native head are authenticated by
    # the maintained reader BEFORE entering the shared fiscal/admission checks.
    payload, generation, head = observe(store, include_head=True)
    original = SimpleNamespace(state=payload, store=SimpleNamespace(candidate=store.candidate,
        bucket=store.bucket, table=store.table, generation=generation))
    admission = _verify_retained_admission(config, original, source_reader=source_reader, clock=clock)
    return {'admission':admission, 'head':head, 'generation':generation}
