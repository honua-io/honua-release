#!/usr/bin/env python3
"""Turn a captured resolver dry-run log into a human and machine-readable verdict."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re


RESOLVED = re.compile(r"^RESOLVED (?P<component>\S+) (?P<sha>[0-9a-f]{7,40})(?:\s|$)")
SKIPS = re.compile(
    r"^SKIPS (?P<component>\S+): selected (?P<sha>[0-9a-f]{7,40}) "
    r"\((?:(?P<age>[0-9.]+) days old|age unknown)\); (?P<skipped>\d+) newer trunk commit\(s\) skipped$")
STALE = re.compile(r"^STALE-CANDIDATE: (?P<component>\S+) selected (?P<sha>[0-9a-f]{7,40}) ")
REFUSED = re.compile(r"^REFUSED: (?P<reason>.+)$")


def missing_server_images(rows: list[dict], resolver_status: int) -> bool:
    refused = [row for row in rows if row['verdict'] != 'resolved']
    if resolver_status != 1 or len(refused) != 1 or refused[0]['component'] != 'honua-server':
        return False
    lines = refused[0]['reason'].splitlines()
    match = re.fullmatch(
        r'honua-server: no qualifying trunk commit in newest (\d+) commits'
        r'((?:; [0-9a-f]{40}: no published SHA-bound image)+)', lines[0])
    if not match or int(match[1]) != match[2].count('; '):
        return False
    diagnostics = (
        r'SKIPS honua-server: no qualifying trunk commit; \d+ commit\(s\) skipped',
        r'  skipped [0-9a-f]{7}: no published SHA-bound image',
        r'  \.\.\. \d+ older skipped commit\(s\) not listed',
    )
    return all(any(re.fullmatch(pattern, line) for pattern in diagnostics) for line in lines[1:])


def parse(text: str, resolver_status: int) -> dict | None:
    rows: dict[str, dict] = {}
    matched = False
    # A qualification refusal is one exception. resolve_trunk_candidate.py prints
    # `REFUSED: {exc}`, and the message continues on the following lines
    # (`candidate qualification refused:` then one component failure per line).
    # Those lines belong to this refusal until the next recognized marker.
    refusal = None
    for line in text.splitlines():
        if match := RESOLVED.match(line):
            refusal = None
            matched = True
            row = rows.setdefault(match['component'], {'component': match['component']})
            # Redirected stdout may arrive after a refusal written to stderr.
            # Selection evidence must never erase that terminal failure.
            row.setdefault('verdict', 'resolved')
            row.update(selected_sha=match['sha'][:7])
        elif match := SKIPS.match(line):
            refusal = None
            matched = True
            row = rows.setdefault(match['component'], {'component': match['component']})
            row.update(selected_sha=match['sha'][:7], age_days=(float(match['age']) if match['age'] else None),
                       newer_commits_skipped=int(match['skipped']))
        elif match := STALE.match(line):
            refusal = None
            matched = True
            row = rows.setdefault(match['component'], {'component': match['component']})
            # The resolver defines staleness as informational, not a refusal.
            row.update(stale=True, selected_sha=match['sha'][:7])
        elif match := REFUSED.match(line):
            matched = True
            reason = match['reason']
            component_match = re.match(r"(?P<component>[\w.-]+):\s", reason)
            component = component_match['component'] if component_match else '(resolver)'
            row = rows.setdefault(component, {'component': component})
            row.update(verdict='refused', reason=reason)
            refusal = row
        elif refusal is not None and line.strip():
            matched = True
            refusal['reason'] += '\n' + line
    if not matched:
        return None
    normalized = []
    for row in rows.values():
        normalized.append({
            'component': row['component'], 'verdict': row.get('verdict', 'refused'),
            'stale': row.get('stale', False),
            'selected_sha': row.get('selected_sha'), 'age_days': row.get('age_days'),
            'newer_commits_skipped': row.get('newer_commits_skipped'), 'reason': row.get('reason'),
        })
    return {'resolves': resolver_status == 0 and all(row['verdict'] == 'resolved' for row in normalized),
            'no_candidate_yet': missing_server_images(normalized, resolver_status), 'rows': normalized}


def markdown(report: dict) -> str:
    lines = ['## Resolver dry run', '',
             '| Component | Verdict | Stale | Selected SHA | Age (days) | Newer commits skipped | Reason |',
             '|---|---|---|---|---:|---:|---|']
    for row in report['rows']:
        value = lambda key: '—' if row[key] is None else str(row[key])
        reason = value('reason').replace('|', '\\|').replace('\n', ' ')
        stale = 'yes' if row['stale'] else '—'
        lines.append(f"| {row['component']} | {row['verdict']} | {stale} | {value('selected_sha')} | "
                     f"{value('age_days')} | {value('newer_commits_skipped')} | {reason} |")
    lines += ['', f"**Resolves:** `{'true' if report['resolves'] else 'false'}`", '']
    if report['no_candidate_yet']:
        lines += ['**Status:** no candidate yet — waiting for a published SHA-bound server image.', '']
    return '\n'.join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('exit_status', type=int)
    parser.add_argument('--json-out', type=Path, required=True)
    args = parser.parse_args(argv)
    report = parse(args.output.read_text(encoding='utf-8'), args.exit_status)
    if report is None:
        print('ERROR: resolver output contained none of the expected line shapes')
        return 2
    rendered = markdown(report)
    print(rendered, end='')
    if summary := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(summary, 'a', encoding='utf-8') as handle:
            handle.write(rendered)
    if output := os.environ.get('GITHUB_OUTPUT'):
        with open(output, 'a', encoding='utf-8') as handle:
            for key in ('resolves', 'no_candidate_yet'):
                handle.write(f"{key}={str(report[key]).lower()}\n")
    args.json_out.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
