#!/usr/bin/env python3
"""Discover and verify a public NuGet artifact for review; never mutate a candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import quote

import yaml
import verify_client_artifacts as verifier


def propose(manifest, client, version):
    artifacts = manifest.get('clientArtifacts') or {}
    current = artifacts.get(client)
    if (not isinstance(current, dict) or current.get('ecosystem') != 'nuget'
            or current.get('registry') != 'nuget.org' or current.get('required', True) is False):
        raise verifier.VerificationError('proposal requires an existing public NuGet client coordinate')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', version):
        raise verifier.VerificationError('an exact package version is required')
    package = current['package']
    package_part, version_part = quote(package.lower(), safe=''), quote(version.lower(), safe='')
    registration = verifier._request_json(f'https://api.nuget.org/v3/registration5-gz-semver2/{package_part}/{version_part}.json')
    catalog_url = str(registration.get('catalogEntry', ''))
    if not catalog_url.startswith('https://api.nuget.org/v3/catalog0/'):
        raise verifier.VerificationError('NuGet returned an untrusted catalog URL')
    catalog = verifier._request_json(catalog_url)
    source = (catalog.get('repository') or {}).get('commit')
    if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{40}', source):
        raise verifier.VerificationError('registry must declare an immutable repository commit')
    filename = f'{package_part}.{version_part}.nupkg'
    url = f'https://api.nuget.org/v3-flatcontainer/{package_part}/{version_part}/{filename}'
    data = verifier._request(url)
    proposed = {**copy.deepcopy(current), 'version': version, 'sourceSha': source,
                'filename': filename, 'digest': verifier._sha256_pin(data), 'publicationState': 'published'}
    identities = verifier.verify_manifest({'clientArtifacts': {client: proposed}}, include_identities=True)
    return {'schema': 'honua.client-artifact-adoption-proposal/v1', 'state': 'review-required',
            'client': client, 'previousArtifact': copy.deepcopy(current), 'proposedArtifact': proposed,
            'verifiedIdentity': identities[client], 'candidateQualification': 'pending',
            'nextSteps': ['review registry-derived artifact adoption', 'regenerate governed requirements through normal automation',
                          'run normal resolver and NIGHTLY-STAGE', 'join exact candidate receipts through NIGHTLY-BIND']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('platform-manifest.yaml'))
    parser.add_argument('--client', required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    result = propose(yaml.safe_load(raw), args.client, args.version)
    result['manifestSha256'] = 'sha256:' + hashlib.sha256(raw).hexdigest()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
