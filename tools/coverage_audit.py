#!/usr/bin/env python3
"""Join coverage findings to the governed catalog without treating inventory as qualification.

Run --output /tmp/coverage-audit.json for all rows; --check verifies the committed
summary. --write-summary deliberately refreshes that summary after source review.
This command never mints receipts, changes scope, or overrides a release verdict.
"""
from __future__ import annotations

import argparse
from collections import Counter
import fnmatch
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = ('surface', 'operation', 'canonical_client', 'client_version', 'deployment_target')
SELECTORS = {'surface', 'operation', 'client_lane', 'capability_key'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return 'sha256:' + hashlib.sha256(canonical(value).encode()).hexdigest()


def load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate JSON key {key!r} in {path}')
            result[key] = value
        return result
    return json.loads(Path(path).read_text(), object_pairs_hook=unique)


def selected(entry, row):
    if any(fnmatch.fnmatchcase(row['client_lane'], p) for p in entry.get('except_client_lanes', [])):
        return False
    return (any(fnmatch.fnmatchcase(row['client_lane'], p) for p in entry.get('client_lanes', []))
            or row['deployment_target'] in entry.get('deployment_targets', []))


def matches(selectors, row):
    return all(any(fnmatch.fnmatchcase(row[key], pattern) for pattern in patterns)
               for key, patterns in selectors.items())


def validate_verification_receipt(receipt, finding_id):
    """Require a traceable, content-bound receipt; qualification still belongs to its consumer."""
    fields = {'url', 'sha256', 'source_revision', 'finding_id'}
    if not isinstance(receipt, dict) or set(receipt) != fields:
        raise ValueError(f'{finding_id}: structured verification receipt required')
    url = receipt['url']
    if not isinstance(url, str) or any(part in {'.', '..'} for part in url.split('/')) or not re.fullmatch(
        r'https://github.com/honua-io/[\w.-]+/(?:'
        r'actions/runs/[1-9]\d*/artifacts/[1-9]\d*|'
        r'blob/[0-9a-f]{40}/[\w./-]+\.json)', url
    ):
        raise ValueError(f'{finding_id}: verification receipt needs an immutable JSON or run artifact URL')
    for field, length in (('sha256', 64), ('source_revision', 40)):
        if not isinstance(receipt[field], str) or not re.fullmatch(f'[0-9a-f]{{{length}}}', receipt[field]):
            raise ValueError(f'{finding_id}: invalid verification receipt {field}')
    if receipt['finding_id'] != finding_id:
        raise ValueError(f'{finding_id}: verification receipt belongs to another finding')


def validate_findings(audit):
    if audit.get('schema') != 'honua.coverage-audit-input/v1':
        raise ValueError('unknown audit schema')
    seen = set()
    for finding in audit['findings']:
        key = finding['id']
        if key in seen:
            raise ValueError(f'duplicate finding {key}')
        seen.add(key)
        for field in ('title', 'owner', 'issue', 'acceptance', 'sources', 'selectors', 'dimension'):
            if not finding.get(field):
                raise ValueError(f'{key}: missing {field}')
        if finding['status'] not in {'open', 'implementation-in-progress', 'implementation-landed', 'verified'}:
            raise ValueError(f'{key}: invalid status')
        if not re.fullmatch(r'https://github.com/honua-io/[\w.-]+/issues/\d+', finding['issue']):
            raise ValueError(f'{key}: issue must be an owning issue URL')
        if set(finding['selectors']) - SELECTORS:
            raise ValueError(f'{key}: unknown selector')
        if any(not isinstance(v, list) or not v or any(not isinstance(p, str) or not p for p in v)
               for v in finding['selectors'].values()):
            raise ValueError(f'{key}: invalid selector values')
        for source in finding['sources']:
            if not re.fullmatch(r'https://github.com/honua-io/[\w.-]+/blob/[0-9a-f]{40}/[^#]+#L\d+(?:-L\d+)?', source):
                raise ValueError(f'{key}: source must pin an immutable commit and line')
        if finding['status'] in {'implementation-landed', 'verified'}:
            if not re.fullmatch(r'https://github.com/honua-io/[\w.-]+/pull/\d+', finding.get('delivery_pr', '')):
                raise ValueError(f'{key}: landed implementation requires a PR')
        # A reviewed source or merged PR is not execution evidence.
        if finding['status'] == 'verified':
            validate_verification_receipt(finding.get('verification_receipt'), key)
    if not seen:
        raise ValueError('audit must retain its findings')


def build(catalog, production, audit):
    validate_findings(audit)
    dispositions = [('registered', p) for p in production['producers']]
    dispositions += [('unproduced', p) for p in production['unproduced']]
    rows, seen, selected_findings = [], set(), Counter()
    for requirement in catalog['requirements']:
        identity = {key: requirement[key] for key in IDENTITY}
        identity_hash = digest(identity)
        if identity_hash in seen:
            raise ValueError(f'duplicate requirement identity: {identity}')
        seen.add(identity_hash)
        if type(requirement['addressable_by_client']) is not bool or type(requirement['licensed']) is not bool:
            raise ValueError('addressable and licensed must be booleans')
        owners = [(kind, item) for kind, item in dispositions if selected(item, requirement)]
        if requirement['addressable_by_client']:
            if len(owners) != 1:
                raise ValueError(f'{identity}: expected exactly one producer disposition, got {len(owners)}')
            registration, producer = owners[0]
        else:
            registration, producer = 'not-addressable', None
            if not requirement.get('addressability_reason'):
                raise ValueError(f'{identity}: not-addressable needs its governed reason')
        reviewed = [f for f in audit['findings'] if matches(f['selectors'], requirement)]
        selected_findings.update(f['id'] for f in reviewed)
        # Selection identifies affected rows, not a completed semantic audit of every row.
        row = {
            **requirement, 'id': identity_hash,
            'audit_scope': 'external-licensed-lane' if requirement['licensed'] else 'in-scope',
            'producer_status': registration,
            'producer': producer.get('producer') if producer else None,
            'producer_issue': producer.get('issue') if producer else None,
            'governed_producer_source': catalog['source_revisions'].get(producer.get('source_revision_key')) if producer else None,
            'findings': [f['id'] for f in reviewed],
            'assertion_review': ('external-licensed-lane' if requirement['licensed'] else
                                 'findings-recorded' if any(f['dimension'] == 'assertions' for f in reviewed)
                                 else 'not-reviewed'),
            'execution': 'not-evaluated',
            'qualification': 'not-evaluated',
        }
        rows.append(row)
    for finding in audit['findings']:
        if selected_findings[finding['id']] == 0 and not finding.get('catalog_gap_reason'):
            raise ValueError(f"{finding['id']}: selects no catalog rows; explicit catalog_gap_reason required")
    rows.sort(key=lambda r: r['id'])
    source_checks = []
    for key, reviewed in sorted(audit['reviewed_sources'].items()):
        if not re.fullmatch('[0-9a-f]{40}', reviewed['commit']):
            raise ValueError(f'{key}: reviewed source must be an immutable SHA')
        governed = catalog['source_revisions'].get(key)
        source_checks.append({'source': key, 'reviewed': reviewed, 'governed': governed,
                              'same_revision': governed == reviewed})
    scoped = [row for row in rows if not row['licensed']]
    summary = {
        'schema': 'honua.coverage-audit-summary/v1',
        'meaning': 'Inventory and source-review status only; never an executed pass rate or release verdict.',
        'requirements_revision': catalog['revision'],
        'requirements_digest': digest(catalog),
        'production_digest': digest(production),
        'audit_digest': digest(audit),
        'rows_digest': digest(rows),
        'rows': len(rows), 'non_licensed_rows': len(scoped),
        'external_licensed_rows': len(rows) - len(scoped),
        'non_licensed_registration': dict(sorted(Counter(r['producer_status'] for r in scoped).items())),
        'non_licensed_assertion_review': dict(sorted(Counter(r['assertion_review'] for r in scoped).items())),
        'finding_matches': dict(sorted(selected_findings.items())),
        'source_comparison': source_checks,
        'execution': 'not-evaluated', 'qualification': 'not-evaluated',
    }
    return {'summary': summary, 'findings': audit['findings'], 'rows': rows,
            'audit_workstreams': audit['workstreams'], 'gate_sequence': audit['gate_sequence']}


def validate_source_artifacts(root, audit):
    for artifact in audit.get('deeper_source_review', {}).get('source_artifacts', []):
        path = Path(artifact['path'])
        if path.is_absolute() or '..' in path.parts or not (root / path).resolve().is_relative_to(root.resolve()):
            raise ValueError('source-review artifact escapes repository')
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError(f'source-review artifact digest mismatch: {path}')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', type=Path)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--check', action='store_true')
    actions.add_argument('--write-summary', action='store_true')
    args = parser.parse_args(argv)
    try:
        audit = load(args.root / 'certification/coverage-audit.v1.json')
        validate_source_artifacts(args.root, audit)
        report = build(load(args.root / 'certification/protocol-certification-requirements.v1.json'),
                       load(args.root / 'certification/sources/protocol-certification-production.v1.json'),
                       audit)
        summary_path = args.root / 'certification/coverage-audit-summary.v1.json'
        if args.check and load(summary_path) != report['summary']:
            raise ValueError('coverage summary drift: review source changes and run --write-summary')
        if args.write_summary:
            summary_path.write_text(json.dumps(report['summary'], indent=2) + '\n')
        if args.output:
            args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report['summary'], indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'coverage audit invalid: {exc}')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
