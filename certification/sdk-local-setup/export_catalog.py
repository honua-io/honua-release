#!/usr/bin/env python3
"""Prepare an independently reviewable expected catalog from exact server source.

This is an offline source projection, never a runtime observation or qualification.
Run under the shared build lock where applicable. The final installer replay remains
the only authority for actual HTTP/stdio parity against the reviewed output.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[1]
PROJECT = Path('tests/dotnet/Honua.Ai.Tests/Honua.Ai.Tests.csproj')
spec = importlib.util.spec_from_file_location('setup_replay', HERE/'run.py')
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def fail(message):
    raise ValueError(message)


def validate_projection(value):
    if value.get('schemaVersion') != 'honua.setup-source-projection/v1' or value.get('view') != 'setup':
        fail('not a source-derived setup projection')
    if not isinstance(value.get('revision'), str) or not value['revision'].strip():
        fail('missing revision')
    for canonical, expected in [('revisionCanonical', 'revisionDigest'),
                                ('membershipCanonical', 'membershipDigest'),
                                ('descriptorJson', 'descriptorDigest')]:
        text = value.get(canonical)
        if not isinstance(text, str) or not text or value.get(expected) != 'sha256:' + digest(text.encode()):
            fail('canonical digest mismatch: ' + canonical)
    tools = value.get('tools')
    if not isinstance(tools, list) or not tools or json.loads(value['descriptorJson']) != tools:
        fail('complete descriptor wire bytes disagree')
    individual = value.get('descriptorJsons')
    if not isinstance(individual, list) or any(not isinstance(item, str) for item in individual) or '[' + ','.join(individual) + ']' != value['descriptorJson']:
        fail('individual descriptor wire bytes disagree')
    names = []
    for tool in tools:
        if not isinstance(tool, dict) or not isinstance(tool.get('name'), str) or not tool['name']:
            fail('tool identity is missing')
        names.append(tool['name'])
        if not isinstance(tool.get('description'), str) or not tool['description']:
            fail('tool description is missing')
        for field in ['inputSchema', 'outputSchema']:
            schema = tool.get(field)
            if not isinstance(schema, dict) or schema.get('type') != 'object':
                fail('complete object schema required: ' + field)
            try:
                Draft202012Validator.check_schema(schema)
            except SchemaError as error:
                raise ValueError('invalid complete schema: ' + field) from error
        if not isinstance(tool.get('annotations'), dict) or not tool['annotations']:
            fail('tool annotations missing')
    if len(set(names)) != len(names):
        fail('duplicate descriptor names')
    members = value.get('members')
    stages = value.get('stages')
    if not isinstance(stages, list) or not stages or len(set(stages)) != len(stages):
        fail('invalid stage roster')
    if not isinstance(members, list) or [m.get('name') for m in members] != names:
        fail('descriptor and membership order disagree')
    if any(m.get('isDynamic') is not False or m.get('stage') not in stages for m in members):
        fail('unexpected dynamic member or stage')
    if set(m['stage'] for m in members) != set(stages):
        fail('missing source stage')
    if value['membershipCanonical'] != ''.join(m['stage'] + '/' + m['name'] + '\n' for m in members):
        fail('membership canonical bytes disagree')
    budgets = value.get('budgets', {})
    for key in ['maxDescriptors', 'maxAggregateBytes', 'maxDescriptorBytes']:
        if type(budgets.get(key)) is not int or budgets[key] <= 0:
            fail('invalid source budget')
    size = len(value['descriptorJson'].encode())
    if value.get('descriptorBytes') != size or len(names) > budgets['maxDescriptors'] or size > budgets['maxAggregateBytes']:
        fail('source descriptor budget mismatch')
    largest = value.get('largestDescriptorBytes')
    if type(largest) is not int or largest != max(len(item.encode()) for item in individual) or not 0 < largest <= min(size, budgets['maxDescriptorBytes']):
        fail('source individual descriptor budget mismatch')
    info = value.get('serverInfo')
    if not isinstance(info, dict) or set(info) != {'name', 'version'} or any(not isinstance(v, str) or not v for v in info.values()):
        fail('invalid source initialize identity')
    return {key: value[key] for key in ['view', 'revision', 'revisionDigest', 'membershipDigest',
                                       'descriptorDigest', 'serverInfo', 'tools']}


def candidate_bindings(platform, platform_sha256, server_source, server_image):
    if not re.fullmatch('[0-9a-f]{40}', server_source) or not re.fullmatch('[0-9a-f]{64}', platform_sha256):
        fail('immutable source and manifest identities required')
    if platform['components']['honua-server']['sha'] != server_source:
        fail('candidate server source disagrees')
    result = {'schemaVersion': 'honua.setup-catalog-manifest/v1',
              'candidateId': 'manifest-sha256:' + platform_sha256, 'serverImage': server_image,
              'packages': []}
    for key, package_name in [('honua-sdk-js', '@honua/sdk-js'), ('honua-mcp-server', '@honua/mcp-server')]:
        pin = platform['clientArtifacts'][key]
        if pin['package'] != package_name or not re.fullmatch(r'\d+\.\d+\.\d+', pin['version']):
            fail('expected stable published client identity')
        if not re.fullmatch(r'sha512-[A-Za-z0-9+/]{86}==', pin['integrity']):
            fail('package integrity is missing')
        result['packages'].append({'name': pin['package'], 'version': pin['version'], 'integrity': pin['integrity']})
    replay.validate_bindings(platform, result, platform_sha256)
    return result


def compose_manifest(projection, platform, platform_sha256, server_source, server_image):
    return {**candidate_bindings(platform, platform_sha256, server_source, server_image),
            **validate_projection(projection)}


def git(checkout, *args):
    return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()


def check_server_source(checkout, expected):
    if not re.fullmatch('[0-9a-f]{40}', expected) or git(checkout, 'rev-parse', 'HEAD') != expected:
        fail('server checkout is not the exact candidate source')
    if git(checkout, 'status', '--porcelain'):
        fail('server checkout is not clean')
    remote = git(checkout, 'remote', 'get-url', 'origin')
    if remote not in ['https://github.com/honua-io/honua-server.git', 'git@github.com:honua-io/honua-server.git']:
        fail('unexpected server repository')
    if not (checkout/PROJECT).is_file():
        fail('canonical source projection project missing')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-checkout', type=Path, required=True)
    parser.add_argument('--server-source', required=True)
    parser.add_argument('--platform-manifest', type=Path, required=True)
    parser.add_argument('--platform-sha256', required=True)
    parser.add_argument('--server-image', required=True, help='Exact shipped immutable installer image string')
    parser.add_argument('--output', type=Path, required=True, help='New private preparation directory; never overwritten')
    args = parser.parse_args()
    os.umask(0o077)
    source = args.server_checkout.resolve()
    check_server_source(source, args.server_source)
    producer_files = [HERE/'export_catalog.py', HERE/'run.py', HERE/'catalog-export/Program.cs',
                      HERE/'catalog-export/CatalogExport.csproj', RELEASE/'tools/verify_client_artifacts.py']
    for path in producer_files:
        committed = subprocess.check_output(['git', '-C', str(RELEASE), 'show',
                                             'HEAD:' + path.relative_to(RELEASE).as_posix()])
        if committed != path.read_bytes():
            fail('exporter code differs from its reviewed commit')
    manifest_bytes = args.platform_manifest.read_bytes()
    if digest(manifest_bytes) != args.platform_sha256:
        fail('platform manifest SHA256 mismatch')
    platform = yaml.safe_load(manifest_bytes)
    candidate_bindings(platform, args.platform_sha256, args.server_source, args.server_image)
    # Verify actual public bytes before allowing source-derived expectations to
    # acquire package bindings. This does not install or qualify those clients.
    sys.path.insert(0, str(RELEASE/'tools'))
    from verify_client_artifacts import verify_manifest
    public = verify_manifest({'clientArtifacts': {key: platform['clientArtifacts'][key]
                              for key in ['honua-sdk-js', 'honua-mcp-server']}},
                             ecosystem='npm', include_identities=True)
    if set(public) != {'honua-sdk-js', 'honua-mcp-server'}:
        fail('both exact public client artifacts must be verified')
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='honua-source-catalog-') as temporary:
        root = Path(temporary)
        env = replay.isolated_env(root)
        # The server build needs its ordinary authenticated NuGet configuration;
        # secrets stay only in this subprocess environment and private logs.
        build_env = {**env, **{k: v for k, v in os.environ.items()
                              if k.startswith('NuGetPackageSourceCredentials_')}}
        commands = [
            ['dotnet', 'build', str(source/PROJECT), '--configuration', 'Release',
             '--no-incremental', '--artifacts-path', str(root/'server')],
            ['dotnet', 'build', str(HERE/'catalog-export/CatalogExport.csproj'),
             '--configuration', 'Release', '--artifacts-path', str(root/'adapter')],
        ]
        for index, command in enumerate(commands):
            with (args.output/f'private-build-{index}.log').open('wb') as log:
                subprocess.run(command, cwd=source, env=build_env, stdout=log,
                               stderr=subprocess.STDOUT, timeout=1800, check=True)
        def built(path, name):
            matches = [p for p in path.rglob(name) if 'bin' in p.parts]
            if len(matches) != 1:
                fail('fresh build did not produce one canonical assembly: ' + name)
            return matches[0]
        tests = built(root/'server', 'Honua.Ai.Tests.dll')
        adapter = built(root/'adapter', 'CatalogExport.dll')
        outputs = []
        for index in range(2):
            output = root/f'projection-{index}.json'
            with (args.output/f'private-export-{index}.log').open('wb') as log:
                subprocess.run(['dotnet', str(adapter), str(tests), str(output)], cwd=source,
                               env=env, stdout=log, stderr=subprocess.STDOUT, timeout=120, check=True)
            outputs.append(output.read_bytes())
        if outputs[0] != outputs[1]:
            fail('source projection is not deterministic')
        check_server_source(source, args.server_source)
        projection = json.loads(outputs[0])
        result = compose_manifest(projection, platform, args.platform_sha256,
                                  args.server_source, args.server_image)
        encoded = (json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()
        receipt = {'schemaVersion': 'honua.setup-source-export/v1',
                   'scope': 'offline expected catalog preparation; not runtime parity or qualification',
                   'serverSource': args.server_source, 'serverTree': git(source, 'rev-parse', 'HEAD^{tree}'),
                   'producerSource': git(RELEASE, 'rev-parse', 'HEAD'),
                   'producerFilesSha256': {p.relative_to(RELEASE).as_posix(): digest(p.read_bytes()) for p in producer_files},
                   'platformManifestSha256': args.platform_sha256,
                   'expectedManifestSha256': digest(encoded), 'projectionSha256': digest(outputs[0]),
                   'assemblySha256': {p.name: digest(p.read_bytes()) for p in tests.parent.glob('*.dll')},
                   'adapterSha256': digest(adapter.read_bytes()), 'publicPackageVerification': public,
                   'reviewed': False}
        (args.output/'source-projection.json').write_bytes(outputs[0])
        (args.output/'expected-setup-manifest.json').write_bytes(encoded)
        (args.output/'preparation.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print('Prepared offline expectations for independent review; no runtime qualification performed.')


if __name__ == '__main__':
    main()
