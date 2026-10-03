#!/usr/bin/env python3
"""Mint a canonical signed nightly lock only for a complete, candidate-bound green train.

Keyless blob signing is separate from publication tag signing: nightly never moves
channels or creates a GA tag. Existing tag_signing validates the release label.

Only the scheduled trunk nightly can mint, and every refusal happens before cosign runs.
Locks live at refs/tags/nightly-lock/<label>; a tag ruleset must forbid deleting or
moving them, or a deleted lock would let the next night reuse its rc number.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

import yaml

from candidate_binding import REQUIRED_RELEASE_GATES, validate_live_report, _sha256
from fixture_revisions import RECORD as FIXTURE_RECORD, declare as declare_fixtures, load as load_fixtures
from generate_platform_lock import generate
from release_notes_ref import snapshot
from release_facts import SOURCE_REFERENCE
from tag_signing import publication_tag
from check_promotion_readiness import EVIDENCE_CLASSES, MAX_FRESHNESS, JOURNEYS, _journey

REQUIRED_NIGHTLY_GATES = REQUIRED_RELEASE_GATES | {'capacity-soak', 'one-operation-rollback', 'journey'}
LABEL = re.compile(r'(?:honua-)?2026\.1-rc\.([1-9][0-9]*)\Z')
LOCK_REFS = 'refs/tags/nightly-lock/'
TRUSTED_REPOSITORY = 'honua-io/honua-release'
TRUSTED_BRANCH = 'trunk'
TRUSTED_WORKFLOW = '.github/workflows/nightly-certification.yml'
SHA = re.compile(r'[0-9a-f]{40}\Z')
DELAYS = (0, 10, 30, 60, 120, 60)
TRANSIENT = ('could not resolve host', 'connection reset', 'connection timed out',
             'operation timed out', 'tls', 'early eof', 'unable to access', 'http 5')
POST_GATE_FIELDS = frozenset({'sbom', 'provenance', 'notes'})
PREDICATES = {'https://slsa.dev/provenance/v0.2': 'provenance',
              'https://slsa.dev/provenance/v1': 'provenance',
              'https://spdx.dev/Document': 'sbom', 'https://cyclonedx.org/bom': 'sbom'}


def artifact_subject(artifact: dict) -> str:
    if artifact.get('integrity'):
        return 'sha512:' + base64.b64decode(artifact['integrity'].removeprefix('sha512-'),
                                          validate=True).hex()
    return artifact.get('digest') or artifact.get('sha256') or ''


# Repository/package-scoped producing workflows. Unlisted publishers must declare the
# exact signer workflow in the candidate manifest; a repository alone is never policy.
PUBLISHER_WORKFLOWS = {
    'honua-io/honua-server': 'nightly-container-build.yml',
    'honua-io/honua-console': 'container-publish.yml',
    'honua-io/honua-sdk-dotnet': 'publish-dotnet-sdk.yml',
    'honua-io/honua-sdk-python': 'publish-python-sdk.yml',
    'honua-io/honua-helm': 'release.yml',
    'honua-io/geospatial-grpc': 'publish-dotnet-protocol.yml',
}
JS_WORKFLOWS = {'@honua/sdk-js': 'publish-js-sdk.yml',
                '@honua/mcp-server': 'publish-mcp-server.yml',
                'create-honua-app': 'publish-create-honua-app.yml'}


def signer_workflow(component: dict, artifact: dict, repository: str) -> str:
    declared = component.get('attestationWorkflow')
    workflow = (JS_WORKFLOWS.get(artifact.get('coordinate')) if repository == 'honua-io/honua-sdk-js'
                else PUBLISHER_WORKFLOWS.get(repository))
    value = declared or (f'{repository}/.github/workflows/{workflow}' if workflow else None)
    if not isinstance(value, str) or not re.fullmatch(
            r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', value):
        raise ValueError(f'{repository}: exact producing attestationWorkflow is not declared or allowlisted')
    return value


def predicate_valid(kind: str, predicate) -> bool:
    """Require document/build contents, rather than accepting a predicate-type label."""
    from jsonschema import Draft202012Validator, FormatChecker
    text = {'type': 'string', 'minLength': 1}
    uri = {**text, 'format': 'uri', 'pattern': r'^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$'}
    def obj(required, properties):
        return {'type': 'object', 'required': required, 'properties': properties}
    if kind == 'https://spdx.dev/Document':
        schema = obj(['spdxVersion', 'SPDXID', 'name', 'documentNamespace', 'dataLicense',
                      'creationInfo', 'packages'], {
            'spdxVersion': {'enum': ['SPDX-2.2', 'SPDX-2.3']},
            'SPDXID': {'const': 'SPDXRef-DOCUMENT'}, 'name': text,
            'documentNamespace': uri, 'dataLicense': {'const': 'CC0-1.0'},
            'creationInfo': obj(['creators', 'created'], {
                'creators': {'type': 'array', 'minItems': 1, 'items': {**text, 'pattern': '^(Person|Organization|Tool): .+'}},
                'created': {'type': 'string', 'format': 'date-time'}}),
            'packages': {'type': 'array', 'minItems': 1, 'items': obj(['SPDXID', 'name'], {
                'SPDXID': {'type': 'string', 'pattern': '^SPDXRef-[A-Za-z0-9.-]+$'}, 'name': text})}})
    elif kind == 'https://cyclonedx.org/bom':
        component = obj(['type', 'name'], {'type': {'enum': ['application', 'framework', 'library',
            'container', 'platform', 'operating-system', 'device', 'firmware', 'file', 'data',
            'machine-learning-model']}, 'name': text})
        schema = obj(['bomFormat', 'specVersion', 'version', 'components'], {
            'bomFormat': {'const': 'CycloneDX'}, 'specVersion': {'enum': ['1.4', '1.5', '1.6', '1.7']},
            'version': {'type': 'integer', 'minimum': 1},
            'components': {'type': 'array', 'minItems': 1, 'items': component}})
    elif kind == 'https://slsa.dev/provenance/v1':
        schema = obj(['buildDefinition', 'runDetails'], {
            'buildDefinition': obj(['buildType', 'externalParameters'], {
                'buildType': uri, 'externalParameters': {'type': 'object'},
                'internalParameters': {'type': 'object'}, 'resolvedDependencies': {'type': 'array',
                    'items': {'type': 'object'}}}),
            'runDetails': obj(['builder'], {'builder': obj(['id'], {'id': uri}),
                'metadata': obj([], {'invocationId': text, 'startedOn': {'type': 'string',
                    'format': 'date-time'}, 'finishedOn': {'type': 'string', 'format': 'date-time'}})})})
    elif kind == 'https://slsa.dev/provenance/v0.2':
        # BuildKit emits the prior SLSA version: its equivalent build/run structures
        # are invocation and builder/metadata, not v1's buildDefinition/runDetails.
        schema = obj(['builder', 'buildType', 'invocation', 'metadata'], {
            'builder': obj(['id'], {'id': uri}), 'buildType': uri,
            'invocation': obj(['parameters'], {'parameters': {'type': 'object'},
                'configSource': {'type': 'object'}}),
            'metadata': {**obj([], {'buildInvocationId': text, 'buildInvocationID': text}),
                         'anyOf': [{'required': ['buildInvocationId']}, {'required': ['buildInvocationID']}]}})
    else:
        return False
    return Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(predicate)


def statement_field(statement: dict, subjects: set[str]) -> str | None:
    """Keep well-formed SBOM/provenance statements naming exact published bytes."""
    if not isinstance(statement, dict):
        return None
    kind = statement.get('predicateType')
    field = PREDICATES.get(kind) if isinstance(kind, str) else None
    rows = statement.get('subject')
    if not field or not isinstance(rows, list) or not rows:
        return None
    named = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('digest'), dict) or not row['digest']:
            return None
        for algorithm, digest in row['digest'].items():
            length = {'sha256': 64, 'sha512': 128}.get(algorithm)
            if length is None or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{%d}' % length, digest):
                return None
            named.add(f'{algorithm}:{digest}')
    return field if named & subjects and predicate_valid(kind, statement.get('predicate')) else None


def publisher_attestations(repository: str, digest: str) -> list[dict]:
    from resolve_trunk_candidate import retry
    result = retry(lambda: subprocess.run(
        ['gh', 'api', f'repos/{repository}/attestations/{digest}', '--paginate', '--slurp'],
        capture_output=True, text=True, check=True,
        env={**os.environ, 'NO_COLOR': '1', 'GH_FORCE_TTY': '0'}))
    pages = json.loads(result.stdout)
    if not isinstance(pages, list) or any(not isinstance(page.get('attestations'), list) for page in pages):
        raise ValueError(f'{repository}: malformed publisher attestation response')
    return [row for page in pages for row in page['attestations']]


def published_artifact_bytes(artifact: dict) -> bytes:
    """Fetch the public package and check its locked hash before attestation lookup."""
    from resolve_trunk_candidate import retry
    from verify_client_artifacts import _request, _request_json
    kind, coordinate, version = artifact['kind'], artifact['coordinate'], artifact['version']
    if kind == 'npm':
        metadata = retry(lambda: _request_json('https://registry.npmjs.org/'
            + urllib.parse.quote(coordinate, safe='') + '/' + urllib.parse.quote(version, safe='')))
        url = str((metadata.get('dist') or {}).get('tarball', ''))
        if urllib.parse.urlparse(url).hostname != 'registry.npmjs.org':
            raise ValueError('npm returned an untrusted tarball URL')
    elif kind == 'nuget':
        package, release = urllib.parse.quote(coordinate.lower(), safe=''), urllib.parse.quote(version.lower(), safe='')
        url = f'https://api.nuget.org/v3-flatcontainer/{package}/{release}/{package}.{release}.nupkg'
    elif kind == 'wheel':
        metadata = retry(lambda: _request_json('https://pypi.org/pypi/'
            + urllib.parse.quote(coordinate, safe='') + '/' + urllib.parse.quote(version, safe='') + '/json'))
        matches = [row for row in metadata.get('urls', [])
                   if 'sha256:' + (row.get('digests') or {}).get('sha256', '') == artifact.get('sha256')]
        if len(matches) != 1:
            raise ValueError('PyPI does not publish exactly one wheel with the locked digest')
        url = matches[0]['url']
        if urllib.parse.urlparse(url).hostname not in {'pypi.org', 'files.pythonhosted.org'}:
            raise ValueError('PyPI returned an untrusted wheel URL')
    elif kind in {'spec', 'archive'} and coordinate.startswith('https://github.com/'):
        url = coordinate.replace('/blob/', '/raw/', 1) if kind == 'spec' else coordinate
    else:
        raise ValueError(f'no published-byte reader for {kind}:{coordinate}')
    raw = retry(lambda: _request(url))
    expected = artifact_subject(artifact)
    actual = ('sha512:' + hashlib.sha512(raw).hexdigest() if expected.startswith('sha512:')
              else 'sha256:' + hashlib.sha256(raw).hexdigest())
    if actual != expected:
        raise ValueError('downloaded package does not match its locked hash')
    return raw


def verify_publisher_bundle(raw: bytes | str, bundle: dict, artifact: dict, repository: str, predicate: str) -> None:
    from resolve_trunk_candidate import retry
    workflow = artifact.get('signerWorkflow')
    if not isinstance(workflow, str) or not re.fullmatch(
            r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml', workflow):
        raise ValueError('exact producing signer workflow is required')
    with tempfile.TemporaryDirectory(prefix='verify-publisher-') as directory:
        package = Path(directory) / 'artifact.bin'
        document = Path(directory) / 'attestation.json'
        if isinstance(raw, bytes):
            package.write_bytes(raw)
            target = str(package)
        else:
            target = raw  # A pinned OCI manifest can be verified without pulling runnable layers.
        document.write_text(json.dumps(bundle))
        retry(lambda: subprocess.run(['gh', 'attestation', 'verify', target, '--bundle', str(document),
            '--repo', repository, '--source-digest', artifact['sourceRevision'],
            '--signer-workflow', workflow, '--predicate-type', predicate, '--format', 'json'], capture_output=True, text=True, check=True))


def collect_post_gate(report: dict, manifest: Path, matrix: Path, output: Path, *,
                      github=None, registry=None, attestations=publisher_attestations,
                      artifact_bytes=published_artifact_bytes, verifier=verify_publisher_bundle) -> dict:
    """Collect actual registry statements/bundles; never reuse manifest evidence declarations.

    OCI indexes must cover each runnable child, including the separately deployed Lambda image.
    Package attestations are retained verbatim, including signatures, in the notes Git parent.
    A missing publisher SBOM/provenance is a blocker, never replaced by a platform BOM.
    """
    from resolve_trunk_candidate import GitHub, Registry
    errors = failures(report)
    for path in (manifest, matrix):
        pin = report.get('candidate', {}).get('artifacts', {}).get(path.name) or {}
        if pin.get('sha256') != _sha256(path) or pin.get('size') != path.stat().st_size:
            errors.append(f'no lock minted: report is not bound to {path.name} bytes')
    if errors:
        raise ValueError('\n'.join(errors))
    github = github or GitHub()
    registry = registry or Registry(github)
    draft = generate(manifest, matrix)
    references = {'sbom': [], 'provenance': []}
    documents, pending = {}, []

    def oci(name, coordinate, digest, context, source_repository, source_revision, component):
        parsed = urllib.parse.urlsplit('oci://' + coordinate)
        if (parsed.netloc != 'ghcr.io' or parsed.query or parsed.fragment
                or not re.fullmatch(r'/[a-z0-9._/-]+', parsed.path)
                or any(part in {'', '.', '..'} for part in parsed.path.removeprefix('/').split('/'))):
            raise ValueError(f'{context}: no attestation reader for {coordinate}')
        repo = parsed.path.removeprefix('/')
        index = registry.document(repo, digest, manifest=True)
        children = [child for child in index.get('manifests', [])
                    if (child.get('platform') or {}).get('os') == 'linux']
        subjects = {child['digest'] for child in children} or {digest}
        coverage = {field: set() for field in references}
        # BuildKit stores in-toto layers in attestation manifests in the same immutable index.
        for child in index.get('manifests', []):
            if (child.get('annotations') or {}).get('vnd.docker.reference.type') != 'attestation-manifest':
                continue
            attestation = registry.document(repo, child['digest'], manifest=True)
            for layer in attestation.get('layers', []):
                if layer.get('mediaType') != 'application/vnd.in-toto+json':
                    continue
                statement = registry.document(repo, layer['digest'])
                field = statement_field(statement, subjects)
                if field:
                    named = {f'{algorithm}:{value}' for subject in statement['subject']
                             for algorithm, value in subject['digest'].items()}
                    coverage[field].update(named & subjects)
                    reference = {'component': name, 'uri': f'oci://{coordinate}@{child["digest"]}',
                                 'sha256': child['digest']}
                    if reference not in references[field]:
                        references[field].append(reference)
        # Helm/OCI publishers can use signed GitHub attestations instead of BuildKit index
        # layers. An attestation verified against the index digest covers every named child.
        if any(covered != subjects for covered in coverage.values()):
            source_repo = source_repository.removeprefix('https://github.com/')
            try:
                rows = attestations(source_repo, digest)
                for number, row in enumerate(rows):
                    bundle = row.get('bundle') or {}
                    statement = json.loads(base64.b64decode(
                        (bundle.get('dsseEnvelope') or {}).get('payload', ''), validate=True))
                    field = statement_field(statement, {digest})
                    if not field or coverage[field] == subjects:
                        continue
                    verifier(f'oci://{coordinate}@{digest}', bundle,
                             {'sourceRevision': source_revision, 'signerWorkflow': signer_workflow(
                                 component, {'coordinate': coordinate}, source_repo)},
                             source_repo, statement['predicateType'])
                    path = f'attestations/{name}/{digest.replace(":", "-")}/{field}-oci-{number}.json'
                    documents[path] = json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode()
                    pending.append((field, name, path))
                    coverage[field] = subjects.copy()
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                errors.append(f'{context}: publisher attestations unavailable or unverified: '
                              + str(getattr(exc, 'stderr', None) or exc).strip())
        for field, covered in coverage.items():
            if covered != subjects:
                detail = ('; honua-server .github/workflows/nightly-container-build.yml '
                          'build-lambda-aot must publish SBOM and provenance (inventory: '
                          'provenance: false at line 326; sbom: false at line 327)'
                          if context.endswith('Lambda') else '')
                errors.append(f'{context}: missing {field} for {", ".join(sorted(subjects - covered))}{detail}')

    data = yaml.safe_load(manifest.read_text())
    server = (data.get('components') or {}).get('honua-server') or {}
    deploy = yaml.safe_load(matrix.read_text()).get('deploy') or {}
    lambda_target = 'awsLambda' in (deploy.get('honua-server') or {})
    if lambda_target and not (server.get('awsLambdaImage') and re.fullmatch(
            r'sha256:[0-9a-f]{64}', str(server.get('awsLambdaDigest', '')))):
        errors.append('honua-server Lambda: declared deployment target requires an exact image and digest')
    elif lambda_target or server.get('awsLambdaImage'):
        coordinate = server['awsLambdaImage'].split('@', 1)[0].rsplit(':', 1)[0]
        oci('honua-server', coordinate, server.get('awsLambdaDigest', ''), 'honua-server Lambda',
            server.get('repository', ''), server.get('sha', ''), server)
    for name, component in sorted(draft.lock['components'].items()):
        for number, artifact in enumerate(component['artifacts']):
            context = f'{name}.artifacts[{number}]'
            digest = artifact_subject(artifact)
            if not digest:
                errors.append(f'{context}: cannot bind attestations without a published digest')
                continue
            if artifact['kind'] in {'image', 'oci-chart'}:
                oci(name, artifact['coordinate'], digest, context,
                    component['source']['repository'], artifact.get('sourceRevision', ''),
                    data['components'][name])
                continue
            repo = component['source']['repository'].removeprefix('https://github.com/')
            try:
                raw = artifact_bytes(artifact)
                digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
                rows = attestations(repo, digest)
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                errors.append(f'{context}: publisher attestations unavailable: '
                              + str(getattr(exc, 'stderr', None) or exc).strip())
                continue
            covered = set()
            for number, row in enumerate(rows):
                bundle = row.get('bundle') or {}
                envelope = bundle.get('dsseEnvelope') or {}
                statement = json.loads(base64.b64decode(envelope.get('payload', ''), validate=True))
                field = statement_field(statement, {digest})
                if not field:
                    continue
                try:
                    verified_artifact = {**artifact, 'signerWorkflow': signer_workflow(
                        data['components'][name], artifact, repo)}
                    verifier(raw, bundle, verified_artifact, repo, statement['predicateType'])
                except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                    errors.append(f'{context}: {field} attestation verification refused: '
                                  + str(getattr(exc, 'stderr', None) or exc).strip())
                    continue
                path = f'attestations/{name}/{digest.replace(":", "-")}/{field}-{number}.json'
                documents[path] = json.dumps(bundle, sort_keys=True, separators=(',', ':')).encode()
                pending.append((field, name, path))
                covered.add(field)
            errors.extend(f'{context}: publisher has no {field} attestation for {digest}'
                          for field in references if field not in covered)
    if errors:
        raise ValueError('no lock minted: post-gate reference collection refused:\n' + '\n'.join(errors))
    revision, stored = snapshot(manifest, matrix, report, output, documents)
    for field, name, path in pending:
        references[field].append({'component': name, 'uri': stored[path],
                                  'sha256': stored[path].rsplit('#', 1)[1]})
    references['notes'] = stored[f'release-notes/{report["platform_label"]}.md']
    result = {'candidate': report['candidate'], 'references': references, 'notesRevision': revision}
    (output / 'post-gate-references.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def attach_post_gate(source: Path, references: dict, declarations: dict, scratch: Path) -> Path:
    """Persist produced facts in the shipped root manifest before regenerating its lock."""
    if set(references) != POST_GATE_FIELDS:
        raise ValueError('post-gate regeneration may supply only sbom, provenance and notes')
    data = yaml.safe_load(source.read_text())
    data.setdefault('platformLockEvidence', {}).update(references)
    data['platformLockEvidence']['evidenceDeclarations'] = declarations
    destination = scratch / source.name
    destination.write_text(yaml.safe_dump(data, sort_keys=True), encoding='utf-8')
    return destination


def regenerate_post_gate(source: Path, matrix: Path, frozen: dict, references: dict):
    """Regenerate persisted facts; permit only produced fields and their source-input hash."""
    from platform_lock_bundle import canonical_bytes
    draft = generate(source, matrix)
    if draft.unresolved:
        raise ValueError('no lock minted: unresolved lock facts:\n' + '\n'.join(draft.unresolved))
    allowed = POST_GATE_FIELDS | {'fixtures'}
    before = copy.deepcopy({key: value for key, value in frozen.items() if key not in allowed})
    after = {key: value for key, value in draft.lock.items() if key not in allowed}
    # Only the root manifest's hash is recomputed. The matrix and the source-input
    # denominator must still equal the qualification draft exactly.
    before['sourceInputs']['platformManifest']['sha256'] = draft.lock['sourceInputs']['platformManifest']['sha256']
    if canonical_bytes(before) != canonical_bytes(after):
        raise ValueError('no lock minted: regeneration changed facts outside sbom, provenance and notes, '
                         'fixtures and evidence declarations')
    if canonical_bytes({key: draft.lock[key] for key in POST_GATE_FIELDS}) != canonical_bytes(references):
        raise ValueError('no lock minted: shipped manifest differs from post-gate references')
    for field in ('sbom', 'provenance'):
        for row in draft.lock[field]:
            uri = row['uri']
            pinned = uri.rsplit('@', 1)[-1] if uri.startswith('oci://') else uri.rsplit('#', 1)[-1]
            if row['sha256'] != pinned:
                raise ValueError(f'no lock minted: {field} reference hash differs from its immutable coordinate')
    from validate_platform_lock import validate
    validation_errors = validate(draft.lock).errors
    if validation_errors:
        raise ValueError('no lock minted: ' + '; '.join(validation_errors))
    return draft


def verify_reference_bundle(evidence: dict, bundle: Path, manifest: Path, matrix: Path, report: dict) -> None:
    """Every retained repo reference must exist at the parent revision and hash to its claim."""
    from finalize_release import render_release_notes
    revision = evidence.get('notesRevision', '')
    if not SHA.fullmatch(revision):
        raise ValueError('no lock minted: missing immutable notes revision')
    repository = 'https://github.com/' + report['candidate']['source']['repository']
    refs = evidence['references']
    with tempfile.TemporaryDirectory(prefix='verify-nightly-notes-') as directory:
        def git(*args):
            return subprocess.run(['git', *args], cwd=directory, check=True, capture_output=True).stdout
        git('init', '-q')
        git('fetch', '--no-tags', str(bundle.resolve()), 'HEAD')
        if git('rev-parse', 'FETCH_HEAD').decode().strip() != revision:
            raise ValueError('no lock minted: notes bundle does not contain the declared parent revision')
        reference_list = [refs['notes']]
        reference_list.extend(row['uri'] for field in ('sbom', 'provenance') for row in refs[field]
                              if not row['uri'].startswith('oci://'))
        for ref in reference_list:
            if not isinstance(ref, str) or not SOURCE_REFERENCE.fullmatch(ref):
                raise ValueError('no lock minted: retained documents require repo@rev:path#sha256 references')
            coordinate, digest = ref.rsplit('#', 1)
            prefix = repository + '@' + revision + ':'
            if not coordinate.startswith(prefix):
                raise ValueError('no lock minted: document is outside the retained notes parent')
            path = coordinate.removeprefix(prefix)
            raw = git('show', f'{revision}:{path}')
            if 'sha256:' + hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError('no lock minted: retained document hash differs from its reference')
            if ref == refs['notes']:
                expected = render_release_notes(yaml.safe_load(manifest.read_text()),
                    yaml.safe_load(matrix.read_text()), report['platform_label'], report,
                    report['candidate']['train']['runUrl']).encode('utf-8')
                if raw != expected:
                    raise ValueError('no lock minted: notes were not generated from this candidate')


# Each receipt derives its verdict from the gate that actually consumes that class.
CLASS_GATES = {
    'build-test': 'build-test', 'contract': 'contract', 'sbom': 'sbom',
    'security': 'security', 'upgrade': 'upgrade', 'capacity-soak': 'capacity-soak',
    'dr': 'dr', 'lambda-certification': 'cloud-parity',
    'protocol-ledger': 'protocol-certification',
    'deterministic-journey': 'journey', 'nightly-model-journey': 'journey',
}


def declare_evidence(report: dict, lock: Path, journeys: list[dict]) -> dict:
    """Declare the R21 tiers and retain workflow verdicts; missing journeys stay red.

    The qualification lock is generated before gates run. Minting later requires these
    receipts to match the newly generated lock bytes, so no class can cross candidates.
    """
    report = copy.deepcopy(report)
    completed = datetime.fromisoformat(report['generatedAt'].replace('Z', '+00:00'))
    if completed.tzinfo != timezone.utc:
        raise ValueError('evidence timestamp must be UTC')
    report['generatedAt'] = completed.isoformat().replace('+00:00', 'Z')
    digest = 'sha256:' + _sha256(lock)
    train = report['candidate']['train']
    gates = {row['gate']: row['status'] for row in report['gates']}
    declarations, receipts = {}, {}
    for name, kind in EVIDENCE_CLASSES.items():
        if kind == 'qualifying':
            declarations[name] = {'kind': kind, 'receipt': None, 'freshUntil': None}
            continue
        expiry = (completed + MAX_FRESHNESS[name]).isoformat().replace('+00:00', 'Z')
        receipt = {'class': name, 'kind': kind, 'runId': str(train['runId']),
                   'runAttempt': train['runAttempt'], 'completedAt': completed.isoformat().replace('+00:00', 'Z'),
                   'status': gates.get(CLASS_GATES[name], 'missing'), 'lockDigest': digest,
                   'freshUntil': expiry, 'gate': CLASS_GATES[name]}
        if name in JOURNEYS:
            mode, required = JOURNEYS[name]
            cells = []
            for journey in journeys:
                if (journey.get('status') != 'pass' or str(journey.get('runId')) != str(train['runId'])
                        or str(journey.get('runAttempt')) != str(train['runAttempt'])
                        or journey.get('candidateDigest') != report['candidate']['artifacts']['platform-manifest.yaml']['sha256']):
                    continue
                for row in journey.get('cells', []):
                    attempts = row.get('attempts', [])
                    if (row.get('cell') not in required or row.get('status') != 'pass' or not attempts
                            or any(attempt.get('driver') != mode for attempt in attempts)):
                        continue
                    cells.append({'cell': row['cell'], 'mode': mode, 'attemptCount': len(attempts),
                                  'attempts': [{'attempt': a['number'], 'status': a['status'],
                                                'failureAttribution': a.get('failureAttribution'),
                                                'completedAt': a.get('completedAt'),
                                                'lockDigest': digest} for a in attempts]})
            receipt['cells'] = cells
            if not _journey(receipt, required, mode, digest, completed - timedelta(hours=24), completed):
                receipt['status'] = 'fail'
        declarations[name] = {'kind': kind, 'receipt': f'promotion-receipts/{name}/receipt.json',
                              'freshUntil': expiry}
        receipts[name] = receipt
    report['evidenceClasses'] = list(receipts)
    report['evidenceDeclarations'] = declarations
    report['evidenceReceipts'] = receipts
    return report


def evidence_failures(report: dict, digest: str | None = None) -> list[str]:
    errors = []
    receipts = report.get('evidenceReceipts') or {}
    declarations = report.get('evidenceDeclarations') or {}
    nightly = {name for name, kind in EVIDENCE_CLASSES.items() if kind == 'nightly'}
    if (set(receipts) != nightly or set(report.get('evidenceClasses') or []) != nightly
            or len(report.get('evidenceClasses') or []) != len(nightly)
            or set(declarations) != set(EVIDENCE_CLASSES)):
        return ['no lock minted: missing R21 evidence declarations or receipts']
    train = report.get('candidate', {}).get('train', {})
    for name, kind in EVIDENCE_CLASSES.items():
        declaration = declarations.get(name) or {}
        if kind == 'qualifying':
            if declaration != {'kind': kind, 'receipt': None, 'freshUntil': None}:
                errors.append(f'{name}: qualifying evidence must be produced during burn')
            continue
        receipt = receipts[name]
        try:
            completed = datetime.fromisoformat(receipt['completedAt'].replace('Z', '+00:00'))
            expiry = datetime.fromisoformat(receipt['freshUntil'].replace('Z', '+00:00'))
            valid = (receipt['class'] == name and receipt['kind'] == kind and receipt['status'] == 'pass'
                     and receipt['gate'] == CLASS_GATES[name]
                     and str(receipt['runId']) == str(train['runId'])
                     and receipt['runAttempt'] == train['runAttempt']
                     and receipt['completedAt'] == report['generatedAt']
                     and completed.tzinfo == expiry.tzinfo == timezone.utc
                     and expiry - completed == MAX_FRESHNESS[name]
                     and declaration == {'kind': kind, 'receipt': f'promotion-receipts/{name}/receipt.json',
                                         'freshUntil': receipt['freshUntil']}
                     and (digest is None or receipt['lockDigest'] == digest))
            if name in JOURNEYS:
                mode, required = JOURNEYS[name]
                valid &= _journey(receipt, required, mode, receipt['lockDigest'],
                                  completed - timedelta(hours=24), completed)
            if not valid:
                errors.append(f'{name}: invalid or missing nightly receipt')
        except (ValueError, KeyError, TypeError):
            errors.append(f'{name}: invalid nightly receipt')
    return errors



def bind(*args, **kwargs):
    # Declaration assembly needs only retained workflow data; schema validation belongs
    # to the real lock-binding/signing path, which has the full minting dependencies.
    from platform_lock_bundle import bind as bind_bundle
    return bind_bundle(*args, **kwargs)


def bundle_files(*args, **kwargs):
    from platform_lock_bundle import bundle_files as files
    return files(*args, **kwargs)


def next_label(history: Path) -> str:
    numbers = [2]  # #383: rc.2 was published; the first real nightly cut is rc.3.
    if history.exists():
        for path in history.rglob('platform-lock.json'):
            lock = json.loads(path.read_text())
            match = LABEL.fullmatch(str(lock.get('platform', {}).get('id', '')))
            if match:
                numbers.append(int(match.group(1)))
    return f'2026.1-rc.{max(numbers) + 1}'


def _git(args, cwd, run=subprocess.run, sleep=time.sleep):
    """Run git, retrying only transient network failures, for up to about five minutes."""
    for index, delay in enumerate(DELAYS):
        if delay:
            sleep(delay)
        result = run(['git', *args], cwd=cwd, capture_output=True)
        if result.returncode == 0:
            return result.stdout
        detail = result.stderr.decode(errors='replace') if isinstance(result.stderr, bytes) else str(result.stderr)
        if index == len(DELAYS) - 1 or not any(term in detail.lower() for term in TRANSIENT):
            raise ValueError(f"git {' '.join(args)} failed: {detail.strip()}")
    raise AssertionError('unreachable')


def sync_history(destination: Path, repository: Path, remote='origin', *, run=subprocess.run,
                 sleep=time.sleep) -> dict[str, str]:
    """Extract every published lock. A failed or partial fetch refuses; it never reads as no history."""
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f'lock history directory is not empty: {destination}')
    destination.mkdir(parents=True, exist_ok=True)
    listing = _git(['ls-remote', '--refs', remote, LOCK_REFS + '*'], repository, run, sleep).decode()
    published = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition('\t')
        label = ref.removeprefix(LOCK_REFS)
        if not SHA.fullmatch(sha) or not ref.startswith(LOCK_REFS) or not LABEL.fullmatch(label):
            raise ValueError(f'unexpected lock ref listing: {line!r}')
        published[label] = sha
    if published:
        _git(['fetch', '--no-tags', remote, *(f'+{LOCK_REFS}{label}:{LOCK_REFS}{label}' for label in published)],
             repository, run, sleep)
    for label, sha in sorted(published.items()):
        local = _git(['rev-parse', '--verify', f'{LOCK_REFS}{label}^{{commit}}'], repository, run, sleep)
        if local.decode().strip() != sha:
            raise ValueError(f'{LOCK_REFS}{label}: fetched {local.decode().strip()} but origin lists {sha}')
        data = _git(['show', f'{sha}:platform-lock.json'], repository, run, sleep)
        lock = json.loads(data)
        if lock.get('platform', {}).get('id') != f'honua-{label}':
            raise ValueError(f'{LOCK_REFS}{label}: lock records {lock.get("platform", {}).get("id")!r}')
        (destination / label).mkdir()
        (destination / label / 'platform-lock.json').write_bytes(data)
    return published


def _ref_pattern(pattern: str) -> re.Pattern:
    if pattern == '~ALL':
        return re.compile(r'refs/tags/.*\Z')
    parts = re.split(r'(\*\*|\*|\?)', pattern)
    body = ''.join({'**': '.*', '*': '[^/]*', '?': '[^/]'}.get(part, re.escape(part)) for part in parts)
    return re.compile(body + r'\Z')


def lock_ref_protected(rulesets: list, ref: str) -> bool:
    """True when an active tag ruleset covering `ref` forbids deleting and moving it."""
    for ruleset in rulesets if isinstance(rulesets, list) else []:
        if not isinstance(ruleset, dict) or ruleset.get('target') != 'tag' or ruleset.get('enforcement') != 'active':
            continue
        names = (ruleset.get('conditions') or {}).get('ref_name') or {}
        if not any(_ref_pattern(str(p)).match(ref) for p in names.get('include') or []):
            continue
        if any(_ref_pattern(str(p)).match(ref) for p in names.get('exclude') or []):
            continue
        rules = {rule.get('type') for rule in ruleset.get('rules') or [] if isinstance(rule, dict)}
        if {'deletion', 'update'} <= rules:
            return True
    return False


CHANNEL_TAG = re.compile(r':(?:latest|stable|nightly|2026\.1)(?:["\s,]|$)')


def stamp_release_label(manifest_path: Path, label: str) -> None:
    """Record the next candidate label on the manifest. This creates no publication tag."""
    publication_tag(label)
    manifest = yaml.safe_load(manifest_path.read_text())
    manifest['platformRelease'] = label
    manifest['status'] = 'rc'
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False))


def _fixture_key(fixture) -> tuple:
    fixture = fixture if isinstance(fixture, dict) else {}
    return (str(fixture.get('repository')), str(fixture.get('revision')), str(fixture.get('path', '')))


def declared_manifest(manifest: Path, fixtures: list[dict], scratch: Path) -> Path:
    """The manifest whose `platformLockEvidence.fixtures` is the gates' declaration (#231 WI-8).

    A candidate that already declares fixtures keeps its exact bytes, but only when it names exactly
    the revisions the gates used: a hand-typed or stale declaration never survives into the lock.
    Otherwise the declaration is written into a copy under `scratch`, which the lock then names.
    """
    data = yaml.safe_load(manifest.read_text(encoding='utf-8'))
    evidence = (data.get('platformLockEvidence') or {}) if isinstance(data, dict) else None
    if not isinstance(evidence, dict):
        raise ValueError('no lock minted: platformLockEvidence must be a mapping')
    data['platformLockEvidence'] = evidence
    if 'fixtures' in evidence:
        stated = evidence['fixtures'] if isinstance(evidence['fixtures'], list) else [evidence['fixtures']]
        if sorted(map(_fixture_key, stated)) != sorted(map(_fixture_key, fixtures)):
            raise ValueError('no lock minted: candidate fixture declaration differs from the revisions '
                             f'the gates used: declared {stated}, gates used {fixtures}')
        return manifest
    evidence['fixtures'] = fixtures
    copy_path = scratch / manifest.name
    copy_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding='utf-8')
    return copy_path


def failures(report: dict, *, repository=TRUSTED_REPOSITORY, source_sha=None, run_id=None) -> list[str]:
    errors = []
    ok, why = validate_live_report(report)
    if not ok:
        errors.append(why)
    candidate = report.get('candidate') if isinstance(report.get('candidate'), dict) else {}
    source = candidate.get('source') if isinstance(candidate.get('source'), dict) else {}
    train = candidate.get('train') if isinstance(candidate.get('train'), dict) else {}
    # A branch can stub its own gates green, so only the reviewed trunk nightly may mint.
    if source.get('repository') != repository:
        errors.append(f"source repository {source.get('repository')!r} is not {repository}")
    if source.get('branch') != TRUSTED_BRANCH:
        errors.append(f"source branch {source.get('branch')!r} is not {TRUSTED_BRANCH}")
    if train.get('workflowPath') != TRUSTED_WORKFLOW:
        errors.append(f"train workflow {train.get('workflowPath')!r} is not {TRUSTED_WORKFLOW}")
    if train.get('certificationMode') != 'live':
        errors.append(f"train certificationMode {train.get('certificationMode')!r} is not live")
    if source_sha is not None and source.get('sha') != source_sha:
        errors.append(f"source sha {source.get('sha')!r} is not this run's {source_sha}")
    if run_id is not None and str(train.get('runId')) != str(run_id):
        errors.append(f"train run {train.get('runId')!r} is not this run {run_id}")
    rows = report.get('gates', [])
    names = {row.get('gate') for row in rows if isinstance(row, dict)}
    errors.extend(f'{name}: missing required gate' for name in sorted(REQUIRED_NIGHTLY_GATES - names))
    errors.extend(f"{row.get('gate', '<unnamed>')}: {row.get('status', '<missing>')} ({row.get('why', '')})"
                  for row in rows if isinstance(row, dict) and row.get('status') != 'pass')
    return errors


def sign_blob(lock_path: Path, bundle_path: Path, identity: str, issuer: str) -> None:
    subprocess.run(['cosign', 'sign-blob', '--yes', '--bundle', str(bundle_path), str(lock_path)], check=True)
    subprocess.run(['cosign', 'verify-blob', '--bundle', str(bundle_path),
                    '--certificate-identity', identity, '--certificate-oidc-issuer', issuer,
                    str(lock_path)], check=True)


def mint(report: dict, manifest: Path, matrix: Path, history: Path, output: Path,
         identity: str, issuer='https://token.actions.githubusercontent.com', *, signer=sign_blob,
         rulesets=None, published=None, repository=TRUSTED_REPOSITORY, source_sha=None, run_id=None,
         fixture_records=(), post_gate_evidence=None, qualification_lock=None, notes_bundle=None) -> str:
    from platform_lock_bundle import canonical_bytes

    errors = failures(report, repository=repository, source_sha=source_sha, run_id=run_id)
    errors.extend(evidence_failures(report))
    try:
        fixtures = declare_fixtures(list(fixture_records), run_id=run_id)
    except ValueError as exc:
        errors.append(str(exc))
    if errors:
        raise ValueError('no lock minted:\n' + '\n'.join(errors))
    candidate = report.get('candidate') or {}
    pins = candidate.get('artifacts') or {}
    for path in (manifest, matrix):
        record = pins.get(path.name) or {}
        if record.get('sha256') != _sha256(path) or record.get('size') != path.stat().st_size:
            raise ValueError(f'no lock minted: report is not bound to {path.name} bytes')
    label = next_label(history)
    publication_tag(label)  # Reuse the ruled platform label syntax; create no publication tag.
    if report.get('platform_label') != label:
        raise ValueError(f'no lock minted: report label must be next candidate {label}')
    if published is None or label in published:
        raise ValueError(f'no lock minted: {LOCK_REFS}{label} is not known to be unpublished')
    if not lock_ref_protected(rulesets, LOCK_REFS + label):
        raise ValueError(f'no lock minted: no active tag ruleset forbids deleting or moving {LOCK_REFS}{label}')
    with tempfile.TemporaryDirectory(prefix='.fixtures-') as scratch:
        from platform_lock_bundle import bind_qualification
        if qualification_lock is None or post_gate_evidence is None:
            raise ValueError('no lock minted: qualification lock and post-gate references are required')
        frozen = json.loads(qualification_lock.read_bytes())
        bind_qualification(frozen, manifest, matrix, label)
        if qualification_lock.read_bytes() != canonical_bytes(frozen):
            raise ValueError('no lock minted: qualification lock must use canonical bytes')
        source = declared_manifest(manifest, fixtures, Path(scratch))
        errors = evidence_failures(report, 'sha256:' + _sha256(qualification_lock))
        if errors:
            raise ValueError('no lock minted:\n' + '\n'.join(errors))
        if canonical_bytes(post_gate_evidence.get('candidate')) != canonical_bytes(report['candidate']):
            raise ValueError('no lock minted: post-gate references belong to another candidate or run')
        source = attach_post_gate(source, post_gate_evidence['references'],
                                  report['evidenceDeclarations'], Path(scratch))
        draft = regenerate_post_gate(source, matrix, frozen, post_gate_evidence['references'])
        bind(draft.lock, source, matrix, label)
        if notes_bundle is None:
            raise ValueError('no lock minted: retained notes bundle is required')
        verify_reference_bundle(post_gate_evidence, notes_bundle, manifest, matrix, report)
        # The green receipts described the freeze lock. Only after checking that binding and the
        # allowed post-gate delta can their lock digest be retargeted. No status/time/class is changed.
        qualification_report = report
        report = copy.deepcopy(report)
        digest = 'sha256:' + hashlib.sha256(canonical_bytes(draft.lock)).hexdigest()
        for receipt in report['evidenceReceipts'].values():
            receipt['lockDigest'] = digest
            for cell in receipt.get('cells', []):
                for attempt in cell.get('attempts', []):
                    attempt['lockDigest'] = digest
        if sorted(map(_fixture_key, draft.lock['fixtures'])) != sorted(map(_fixture_key, fixtures)):
            raise ValueError('no lock minted: lock fixtures differ from the revisions the gates used')
        if output.exists():
            raise ValueError(f'no lock minted: immutable output already exists: {output}')
        output.parent.mkdir(parents=True, exist_ok=True)
        # Failed generation, signing or verification leaves neither a lock nor a partial bundle.
        with tempfile.TemporaryDirectory(prefix='.nightly-', dir=output.parent) as directory:
            staging = Path(directory) / label
            staging.mkdir()
            for name, data in bundle_files(draft.lock).items():
                (staging / name).write_bytes(data)
            if CHANNEL_TAG.search((staging / 'platform-lock.json').read_text()):
                raise ValueError('no lock minted: lock contains a channel tag')
            digest = 'sha256:' + hashlib.sha256((staging / 'platform-lock.json').read_bytes()).hexdigest()
            errors = evidence_failures(report, digest)
            if errors:
                raise ValueError('no lock minted:\n' + '\n'.join(errors))
            retained_report = copy.deepcopy(report)
            if source != manifest:
                # Promotion consumes the root manifest and verifies its report binding. Preserve
                # the qualified inputs before adding the gates' observed declarations.
                qualified = staging / 'qualification-inputs'
                qualified.mkdir()
                (qualified / manifest.name).write_bytes(manifest.read_bytes())
                (qualified / 'gate-report.json').write_bytes(canonical_bytes(qualification_report))
                (staging / manifest.name).write_bytes(source.read_bytes())
                retained_report['candidate']['artifacts'][manifest.name] = {
                    **pins[manifest.name], 'sha256': _sha256(source), 'size': source.stat().st_size}
            (staging / 'gate-report.json').write_bytes(canonical_bytes(retained_report))
            (staging / 'post-gate-references.json').write_bytes(canonical_bytes(post_gate_evidence))
            (staging / 'qualification-lock.json').write_bytes(qualification_lock.read_bytes())
            (staging / 'qualification-gate-report.json').write_bytes(canonical_bytes(qualification_report))
            (staging / 'release-notes.bundle').write_bytes(notes_bundle.read_bytes())
            (staging / 'release-notes-revision.txt').write_text(post_gate_evidence['notesRevision'] + '\n')
            for name, receipt in report['evidenceReceipts'].items():
                path = staging / 'promotion-receipts' / name / 'receipt.json'
                path.parent.mkdir(parents=True)
                path.write_bytes(canonical_bytes(receipt))
            # Retain the gate records behind $.fixtures alongside the canonical candidate inputs.
            (staging / FIXTURE_RECORD).write_bytes(canonical_bytes(
                {'fixtures': fixtures, 'records': sorted(fixture_records, key=lambda r: (r['gate'], r['job']))}))
            signer(staging / 'platform-lock.json', staging / 'platform-lock.sigstore.json', identity, issuer)
            if not (staging / 'platform-lock.sigstore.json').is_file():
                raise ValueError('no lock minted: signer returned no signature bundle')
            staging.rename(output)
    return label


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--declare-evidence', action='store_true')
    parser.add_argument('--collect-post-gate', action='store_true')
    parser.add_argument('--post-gate-references', type=Path)
    parser.add_argument('--notes-bundle', type=Path)
    parser.add_argument('--lock', type=Path)
    parser.add_argument('--journey-reports', type=Path)
    parser.add_argument('--history', type=Path, default=Path('nightly-history'))
    parser.add_argument('--next-label', action='store_true')
    parser.add_argument('--stamp', type=Path, help='write the next label onto this candidate manifest')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--matrix', type=Path)
    parser.add_argument('--out-dir', type=Path, default=Path('nightly-lock'))
    parser.add_argument('--certificate-identity')
    parser.add_argument('--sync-from', metavar='REMOTE',
                        help='first extract every published lock from this git remote into --history')
    parser.add_argument('--rulesets', type=Path, help='JSON list of the repository rulesets')
    parser.add_argument('--expected-repository', default=TRUSTED_REPOSITORY)
    parser.add_argument('--expected-source-sha')
    parser.add_argument('--expected-run-id')
    parser.add_argument('--fixture-revisions', type=Path,
                        help='directory holding every fixture gate\'s fixture-revisions.json from this run')
    args = parser.parse_args(argv)
    try:
        if args.collect_post_gate:
            if not all((args.report, args.manifest, args.matrix)):
                raise ValueError('post-gate collection requires report, manifest and matrix')
            collect_post_gate(json.loads(args.report.read_text()), args.manifest, args.matrix, args.out_dir)
            return 0
        if args.declare_evidence:
            if not args.report or not args.lock or not args.journey_reports:
                raise ValueError('declaration requires report, lock and journey reports')
            journeys = [json.loads(path.read_text()) for path in args.journey_reports.rglob('gate-report-journey.json')]
            report = declare_evidence(json.loads(args.report.read_text()), args.lock, journeys)
            args.report.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            return 0
        published = None
        if args.sync_from:
            published = sync_history(args.history, Path.cwd(), args.sync_from)
        if args.stamp:
            if published is None:
                raise ValueError('stamping requires --sync-from so the published lock history is complete')
            label = next_label(args.history)
            stamp_release_label(args.stamp, label)
            print(label)
        elif args.next_label:
            print(next_label(args.history))
        else:
            if not all((args.report, args.manifest, args.matrix, args.certificate_identity, args.rulesets,
                        args.expected_source_sha, args.expected_run_id, args.fixture_revisions,
                        args.lock, args.post_gate_references, args.notes_bundle)):
                raise ValueError('report, candidate inputs, trusted signing identity, rulesets, '
                                 'the expected source sha and run id, and fixture revisions are required; '
                                 'qualification lock and post-gate references are required')
            if published is None:
                raise ValueError('minting requires --sync-from so the published lock history is complete')
            label = mint(json.loads(args.report.read_text()), args.manifest, args.matrix,
                         args.history, args.out_dir, args.certificate_identity,
                         rulesets=json.loads(args.rulesets.read_text()), published=published,
                         repository=args.expected_repository, source_sha=args.expected_source_sha,
                         run_id=args.expected_run_id,
                         fixture_records=load_fixtures(args.fixture_revisions),
                         post_gate_evidence=json.loads(args.post_gate_references.read_text()),
                         qualification_lock=args.lock, notes_bundle=args.notes_bundle)
            print(f'MINTED: {label} -> {args.out_dir}')
        return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'REFUSED: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
