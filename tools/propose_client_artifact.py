#!/usr/bin/env python3
"""Discover and verify a published client artifact for review; never mutate a candidate."""
from __future__ import annotations

import argparse
import base64
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
    if not isinstance(current, dict) or current.get('required', True) is False:
        raise verifier.VerificationError('proposal requires an existing required client coordinate')
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?', version):
        raise verifier.VerificationError('an exact package version is required')
    ecosystem = current.get('ecosystem')
    proposed = {**copy.deepcopy(current), 'version': version, 'publicationState': 'published'}
    package = current['package']
    package_part, version_part = quote(package.lower(), safe=''), quote(version.lower(), safe='')
    if ecosystem == 'nuget' and current.get('registry') == 'nuget.org':
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
        proposed.update(sourceSha=source, filename=filename, digest=verifier._sha256_pin(data))
    elif ecosystem == 'npm':
        metadata = verifier._request_json(f'https://registry.npmjs.org/{quote(package, safe="")}/{quote(version, safe="")}')
        dist = metadata.get('dist') or {}
        url = str(dist.get('tarball', ''))
        if not url.startswith('https://registry.npmjs.org/'):
            raise verifier.VerificationError('npm returned an untrusted tarball URL')
        source = metadata.get('gitHead')
        if source is None:
            # This is only an untrusted discovery hint. verify_manifest below
            # must authenticate its complete publisher bundle before output.
            provenance = verifier._request_json('https://registry.npmjs.org/-/npm/v1/attestations/'
                + quote(package, safe='') + '@' + quote(version, safe=''))
            bundles = [row.get('bundle') for row in provenance.get('attestations', [])
                       if row.get('predicateType') == 'https://slsa.dev/provenance/v1']
            if len(bundles) != 1:
                raise verifier.VerificationError('npm requires one publisher source discovery bundle')
            try:
                statement = json.loads(base64.b64decode(bundles[0]['dsseEnvelope']['payload'], validate=True))
                dependencies = statement['predicate']['buildDefinition']['resolvedDependencies']
                if len(dependencies) != 1:
                    raise ValueError('ambiguous publisher source')
                source = dependencies[0]['digest']['gitCommit']
            except (KeyError, TypeError, ValueError) as exc:
                raise verifier.VerificationError('npm source discovery bundle is invalid') from exc
        proposed.update(sourceSha=source, integrity=dist.get('integrity'), filename=url.rsplit('/', 1)[-1])
    elif ecosystem == 'pypi':
        metadata = verifier._request_json(f'https://pypi.org/pypi/{quote(package, safe="")}/{quote(version, safe="")}/json')
        wheels = [row for row in metadata.get('urls', []) if row.get('packagetype') == 'bdist_wheel']
        if len(wheels) != 1:
            raise verifier.VerificationError('PyPI requires exactly one published wheel')
        wheel = wheels[0]
        proposed.update(filename=wheel['filename'], digest='sha256:' + wheel['digests']['sha256'])
        source = verifier._pypi_published_source(client, proposed)
        proposed['sourceSha'] = source
    else:
        raise verifier.VerificationError('proposal requires npm, PyPI or public NuGet')
    if not isinstance(source, str) or not re.fullmatch(r'[0-9a-f]{40}', source):
        raise verifier.VerificationError('registry must declare an immutable repository commit')
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
