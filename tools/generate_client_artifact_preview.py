#!/usr/bin/env python3
"""Generate reviewed artifact/requirements previews outside the repository; never bind a candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import yaml
import convergence_rebind as convergence
import propose_client_artifact as proposal
import resolve_trunk_candidate as resolver


class PreviewError(ValueError):
    pass


def digest(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def replace_mappings(text, replacements):
    """Replace generated mapping blocks while preserving all other YAML bytes."""
    tree = yaml.compose(text)
    changes = []
    for path, value in replacements.items():
        node = tree
        for name in path:
            matches = [(key, child) for key, child in node.value if key.value == name]
            if len(matches) != 1:
                raise PreviewError(f'missing or duplicate manifest path {path}')
            key, node = matches[0]
        start = key.start_mark.index - key.start_mark.column
        end = node.end_mark.index - node.end_mark.column
        block = yaml.safe_dump({path[-1]: value}, sort_keys=False, allow_unicode=True)
        indent = ' ' * key.start_mark.column
        block = ''.join(indent + line if line.strip() else line for line in block.splitlines(keepends=True))
        changes.append((start, end, block + '\n'))
    for start, end, block in sorted(changes, reverse=True):
        text = text[:start] + block + text[end:]
    return text


def project_manifest(raw, proposals, github):
    original = yaml.safe_load(raw)
    candidate = copy.deepcopy(original)
    replacements = {}
    seen = set()
    for item in proposals:
        client = item.get('client')
        if (item.get('schema') != 'honua.client-artifact-adoption-proposal/v1'
                or item.get('state') != 'review-required' or item.get('candidateQualification') != 'pending'
                or item.get('manifestSha256') != digest(raw) or client in seen):
            raise PreviewError('proposal schema/state/manifest hash or duplicate client mismatch')
        seen.add(client)
        if item.get('previousArtifact') != original.get('clientArtifacts', {}).get(client):
            raise PreviewError('proposal previous artifact differs from the exact manifest')
        version = (item.get('proposedArtifact') or {}).get('version')
        fresh = proposal.propose(original, client, version)
        if any(fresh[key] != item.get(key) for key in ('proposedArtifact', 'verifiedIdentity')):
            raise PreviewError('reviewed proposal differs from live verified registry identity')
        artifact = fresh['proposedArtifact']
        candidate['clientArtifacts'][client] = artifact
        replacements[('clientArtifacts', client)] = artifact
        coordinate = f"{artifact['ecosystem']}:{artifact['package']}"
        components = [(name, component) for name, component in original['components'].items()
                      if component.get('artifact') == coordinate]
        if len(components) > 1:
            raise PreviewError('ambiguous primary SDK component')
        if components:
            name, component = components[0]
            if name not in resolver.SDK_COMPONENTS:
                raise PreviewError('artifact coordinate belongs to an unexpected component')
            selected = resolver.select_sdk(name, component, candidate['clientArtifacts'],
                                           {client: fresh['verifiedIdentity']}, github)
            selected.update(resolver.component_versions(github, name, selected))
            candidate['components'][name] = selected
            replacements[('components', name)] = selected
    if not seen:
        raise PreviewError('at least one reviewed proposal is required')
    text = replace_mappings(raw.decode('utf-8'), replacements)
    if yaml.safe_load(text) != candidate:
        raise PreviewError('generated YAML differs from the exact allowed projection')
    for key in ('honua-server',):
        if candidate['components'].get(key) != original['components'].get(key):
            raise PreviewError('frozen server identity changed')
    if candidate.get('protocolCertification') != original.get('protocolCertification'):
        raise PreviewError('protocol ledger or binding changed')
    return text.encode(), candidate


def requirement_diff(old, new):
    fields = ('capability_key', 'surface', 'operation', 'canonical_client', 'client_lane', 'deployment_target')
    def index(rows):
        result = {}
        for row in rows:
            key = tuple(row.get(field) for field in fields)
            if key in result:
                raise PreviewError('ambiguous requirement identity')
            result[key] = row
        return result
    before, after = index(old['requirements']), index(new['requirements'])
    removed = [before[key] for key in sorted(before.keys() - after.keys(), key=repr)]
    added = [after[key] for key in sorted(after.keys() - before.keys(), key=repr)]
    changed = [{'before': before[key], 'after': after[key]} for key in sorted(before.keys() & after.keys(), key=repr)
               if before[key] != after[key]]
    return {'originalRows': len(before), 'proposedRows': len(after), 'removedObligations': removed,
            'addedUnqualifiedObligations': added, 'changedObligations': changed,
            'qualification': 'pending; source projections establish no execution proof'}


def source_tier_diff(root, payloads):
    path = 'certification/sources/sdk-dotnet/sdk-certification.v1.json'
    if path not in payloads:
        return []
    before = {row['id']: row for row in json.loads((root / path).read_bytes())['operations']}
    after = {row['id']: row for row in json.loads(payloads[path])['operations']}
    return [{'operation': key, 'before': before.get(key), 'after': after.get(key), 'qualification': 'pending'}
            for key in sorted(before.keys() | after.keys())
            if key not in before or key not in after
            or before[key].get('requiredTiers') != after[key].get('requiredTiers')]


def generate(root, output, proposals, github=None, stage_github=None):
    root, output = root.resolve(), output.resolve()
    if output == root or root in output.parents or output.exists():
        raise PreviewError('output must be nonexistent and outside the source repository')
    raw = (root / 'platform-manifest.yaml').read_bytes()
    manifest, _ = project_manifest(raw, proposals, github or resolver.GitHub())
    rehearsal = (root / 'customer-install-manifest.json').read_bytes()
    with tempfile.TemporaryDirectory(prefix='artifact-preview-') as temporary:
        trial = Path(temporary) / 'repo'
        shutil.copytree(root, trial, ignore=shutil.ignore_patterns('.git', '__pycache__', '.venv',
                                                                  'node_modules', 'artifacts'))
        (trial / 'platform-manifest.yaml').write_bytes(manifest)
        plan, payloads, _ = convergence.prepare(trial, stage_github or convergence.GhCli(), 'keep')
        old = json.loads((root / convergence.CATALOG).read_bytes())
        new = json.loads(payloads[str(convergence.CATALOG)])
        comparison = requirement_diff(old, new)
        tiers = source_tier_diff(root, payloads)
        for change in comparison['changedObligations']:
            before, after = change['before'], change['after']
            if not set(before.get('scenario_facets', [])) <= set(after.get('scenario_facets', [])):
                raise PreviewError('generated catalog loses required scenario facets: ' + json.dumps(change, sort_keys=True))
            ranks = {'pr': 0, 'nightly': 1, 'release': 2}
            if ranks.get(after.get('required_tier'), 99) > ranks.get(before.get('required_tier'), 99):
                raise PreviewError('generated catalog weakens an original required tier: ' + json.dumps(change, sort_keys=True))
            for field in ('auth_policy_revision', 'maturity', 'licensed', 'addressable_by_client', 'entitlement_policy_revision'):
                if before.get(field) != after.get(field):
                    raise PreviewError('generated catalog requires explicit semantic policy review: ' + json.dumps(change, sort_keys=True))
        lost_tiers = [change for change in tiers if change['before'] is not None and
                      not set(change['before'].get('requiredTiers', [])) <=
                          set((change['after'] or {}).get('requiredTiers', []))]
        if lost_tiers:
            raise PreviewError('source projection loses original tier obligations: ' + json.dumps(lost_tiers, sort_keys=True))
        if any(not str(path).startswith('certification/') for path in payloads):
            raise PreviewError('normal preparation returned an unexpected output path')
        if comparison['removedObligations'] or len(new['requirements']) < len(old['requirements']):
            raise PreviewError('generated catalog loses original obligations; explicit correction/review is required')
        if (trial / 'customer-install-manifest.json').read_bytes() != rehearsal:
            raise PreviewError('customer rehearsal binding changed')
        if (root / 'customer-install-manifest.json').read_bytes() != rehearsal:
            raise PreviewError('source customer rehearsal changed during generation')
        if (root / 'platform-manifest.yaml').read_bytes() != raw:
            raise PreviewError('source manifest changed during generation')
        report = {'schema': 'honua.client-artifact-adoption-preview/v1', 'state': 'review-required',
                  'candidateAdmission': False, 'manifestSha256': digest(raw),
                  'proposalClients': [item['client'] for item in proposals], 'requirements': comparison,
                  'sourceTierObligations': tiers, 'normalPreparationPlan': plan,
                  'customerRehearsalSha256': digest(rehearsal),
                  'next': 'review exact source/semantic drift; normal protected PR and actual certification remain required'}
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.artifact-preview-', dir=output.parent) as staging:
            destination = Path(staging) / 'result'
            destination.mkdir()
            files = {'platform-manifest.yaml': manifest, **payloads,
                     'artifact-adoption-review.json': (json.dumps(report, indent=2) + '\n').encode()}
            for path, data in files.items():
                relative = Path(path)
                if relative.is_absolute() or '..' in relative.parts:
                    raise PreviewError('normal preparation returned an unsafe output path')
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (destination / 'preview-inventory.json').write_text(json.dumps(
                {path: digest(data) for path, data in sorted(files.items())}, indent=2) + '\n')
            if output.exists():
                raise PreviewError('output appeared during generation')
            destination.rename(output)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--proposal', type=Path, action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = generate(args.root, args.output, [json.loads(path.read_bytes()) for path in args.proposal])
    print(json.dumps({'state': report['state'], 'candidateAdmission': False,
                      'requirements': {key: value for key, value in report['requirements'].items()
                                       if key in ('originalRows', 'proposedRows', 'qualification')}}, indent=2))


if __name__ == '__main__':
    main()
