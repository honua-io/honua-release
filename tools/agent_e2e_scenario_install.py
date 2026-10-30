"""Bind an already running local-docker candidate. This does not install one.

Docker inspect and an independent health read are observations. Privileged
containers and host-socket mounts are refused. Image digests come from the
reviewed candidate provenance, not from container labels.
"""
from __future__ import annotations

import re

from agent_e2e_deployment import identity
from agent_e2e_pack import PackError


def require(value, reason):
    if not value:
        raise PackError(reason)


def bind(*, deployment, campaign, provenance, health):
    require(deployment.get('target') == 'local-docker', 'installation adapter is local-docker only')
    deployment_id = identity(deployment, campaign, 'local-docker')
    container = deployment['nativeResponse'][0]
    require(isinstance(provenance, dict) and set(provenance) == {
        'componentBindingDigest', 'serverSourceRevision', 'serverOciDigest', 'clientArtifactDigests'},
        'installation provenance is incomplete')
    require(provenance['componentBindingDigest'] == campaign['componentBindingDigest'],
            'installation provenance belongs to another candidate')
    require(re.fullmatch(r'[0-9a-f]{40}', provenance['serverSourceRevision']), 'installation source revision is not a commit')
    require(container.get('Image') == provenance['serverOciDigest'] and
            re.fullmatch(r'sha256:[0-9a-f]{64}', provenance['serverOciDigest']),
            'running image differs from reviewed server digest')
    digests = provenance['clientArtifactDigests']
    require(isinstance(digests, dict) and digests and all(
        isinstance(key, str) and key and isinstance(value, str) and re.fullmatch(r'sha256:[0-9a-f]{64}', value)
        for key, value in digests.items()), 'installation client artifacts are not pinned')
    host = container.get('HostConfig') or {}
    require(host.get('Privileged') is False, 'local-docker candidate cannot be privileged')
    binds = host.get('Binds') or []
    require(isinstance(binds, list) and all(isinstance(item, str) and 'docker.sock' not in item for item in binds),
            'local-docker candidate cannot mount the host socket')
    require(isinstance(health, dict) and type(health.get('authenticatedReadStatus')) is int and
            isinstance(health.get('status'), str) and health['status'],
            'installation lacks an independent health observation')
    return {'qualification': False, 'candidate.identity': {
        'componentBindingDigest': provenance['componentBindingDigest'],
        'serverSourceRevision': provenance['serverSourceRevision'],
        'serverOciDigest': provenance['serverOciDigest'],
        'clientArtifactDigests': digests}, 'deployment.ready': {
            'deploymentId': deployment_id, 'healthStatus': health['status'],
            'authenticatedReadStatus': health['authenticatedReadStatus']}}
