"""Local Docker has no native AWS Batch job. Refuse a substitute.

Batch cases stay on aws-batch. This adapter does not mint job ids, shrink the
330,000-row import, or report a local container as a Batch worker.
"""
from __future__ import annotations

from agent_e2e_pack import PackError


def observe(case_id, target):
    if target == 'local-docker' or (isinstance(case_id, str) and case_id.endswith(':local-docker')):
        raise PackError('local-docker has no native AWS Batch job; refusing a local substitute')
    raise PackError('native Batch observation is not implemented for '+str(target))
