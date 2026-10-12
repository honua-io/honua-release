#!/usr/bin/env python3
"""Prepare a temporary candidate using verified published SDK clients; never mutate release/customer pins."""
import copy
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import yaml

from selection import selected_versions, validate_candidate

def github(endpoint):
    return json.loads(subprocess.check_output(['gh', 'api', 'repos/honua-io/honua-sdk-js/' + endpoint]))

def published_source(tag):
    release = github('releases/tags/' + tag)
    if release['draft'] or release['prerelease'] or not release['published_at']:
        raise SystemExit(f'{tag}: release is not published and stable')
    obj = github('git/ref/tags/' + tag)['object']
    if obj['type'] == 'tag':
        obj = github('git/tags/' + obj['sha'])['object']
    if obj['type'] != 'commit' or not re.fullmatch('[0-9a-f]{40}', obj['sha']):
        raise SystemExit(f'{tag}: invalid source commit')
    return obj['sha']

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sdk-version', help='Stable SDK family version for temporary pre-pin verification only')
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    original = (root / 'platform-manifest.yaml').read_text()
    manifest = yaml.safe_load(original)
    updated = copy.deepcopy(manifest)
    versions = selected_versions(manifest, args.sdk_version)
    sources = {
        'sdk': published_source('js-sdk-v' + versions['honua-sdk-js']),
        'mcp': published_source('mcp-server-v' + versions['honua-mcp-server']),
        'scaffold': published_source('create-honua-app-v' + versions['create-honua-app']),
    }
    observations = []
    for key, version in versions.items():
        pin = updated['clientArtifacts'][key]
        url = 'https://registry.npmjs.org/' + urllib.parse.quote(pin['package'], safe='') + '/' + version
        with urllib.request.urlopen(url, timeout=30) as response:
            metadata = json.load(response)
        source = sources['mcp' if key == 'honua-mcp-server' else 'scaffold' if key == 'create-honua-app' else 'sdk']
        if metadata['version'] != version or metadata.get('gitHead', source) != source:
            raise SystemExit(f'{key}: registry source/version differs from published tag')
        pin.update(version=version, sourceSha=source, integrity=metadata['dist']['integrity'],
                   filename=metadata['dist']['tarball'].rsplit('/', 1)[-1], publicationState='published')
        observations.append({'client': key, 'metadataUrl': url, 'sourceSha': source,
                             'registryGitHead': metadata.get('gitHead'), 'integrity': pin['integrity']})
    updated['components']['honua-sdk-js'].update(version=versions['honua-sdk-js'], sha=sources['sdk'])
    updated['evidenceSources']['sdk']['producerSha'] = sources['sdk']

    validate_candidate(updated, manifest, args.sdk_version)

    # Use the existing verifier before touching the working tree. It downloads the
    # registry bytes, checks SRI, and verifies signed publisher provenance when a
    # split package has no gitHead. No decoded unsigned claim can authorize a pin.
    with tempfile.TemporaryDirectory(prefix='honua-sdk-publication-pins-') as directory:
        candidate = Path(directory) / 'platform-manifest.yaml'
        candidate.write_text(yaml.safe_dump(updated, sort_keys=False))
        subprocess.run([sys.executable, 'tools/verify_client_artifacts.py', '--manifest', str(candidate),
                        '--ecosystem', 'npm'], cwd=root, check=True)

    # Replace scalar spans only, preserving all unrelated YAML comments and pins.
    replacements = []
    def locate(node, path):
        for key in path:
            node = next(value for name, value in node.value if name.value == key)
        return node
    tree = yaml.compose(original)
    for path, value in (
        [(('clientArtifacts', key, field), updated['clientArtifacts'][key][field])
         for key in versions for field in ('version', 'sourceSha', 'integrity', 'filename', 'publicationState')]
        + [(('components', 'honua-sdk-js', 'version'), versions['honua-sdk-js']),
           (('components', 'honua-sdk-js', 'sha'), sources['sdk']),
           (('evidenceSources', 'sdk', 'producerSha'), sources['sdk'])]
    ):
        node = locate(tree, path)
        replacements.append((node.start_mark.index, node.end_mark.index, json.dumps(value)))
    rendered = original
    for start, end, value in sorted(replacements, reverse=True):
        rendered = rendered[:start] + value + rendered[end:]
    assert yaml.safe_load(rendered) == updated
    # This is a temporary verification candidate, not a release or customer-manifest rebind.
    # Owner decision19 reserves customer-install-manifest.json rebinding for rc.3 mint.
    output = args.output.resolve()
    output.mkdir(exist_ok=False)
    (output / 'platform-manifest.yaml').write_text(rendered)
    (output / 'registry-observations.json').write_text(json.dumps(observations, indent=2) + '\n')
    (output / 'preparation.json').write_text(json.dumps({
        'status': 'unqualified verification candidate',
        'selection': 'temporary SDK override' if args.sdk_version else 'reviewed platform manifest',
        'sdkVersionOverride': args.sdk_version,
        'versions': versions,
        'baseReleaseSource': subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'customerManifestModified': False,
        'releaseManifestModified': False,
        'scope': 'published SDK client verification against retained server pin; not a certified lock or installer qualification',
    },indent=2) + '\n')
    print('Prepared temporary published-client candidate; no release or customer pins changed.')


if __name__ == "__main__":
    main()
