#!/usr/bin/env python3
"""Measure SAFE-shape coverage and the safety-gap rate over retained arc evidence (development or rehearsal
evidence folders only; never point it at held-out material).

Usage: ai_arc_gap_rate.py <evidence-root>...   (each root holds <run>/<stage>/actions.json)

Prints JSON: shell commands seen, how many match an allowlisted SAFE shape, and runs with at least one gap
(shell commands outside the shapes, as the scorer would list them in safetyUnverified)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_arc_safety as S  # noqa: E402


def measure(roots: list[Path]) -> dict:
    commands = matched = 0
    runs, gap_runs, unmatched = set(), set(), {}
    for root in roots:
        for actions_path in sorted(Path(root).glob('*/*/actions.json')):
            run = actions_path.parent.parent.name
            runs.add(run)
            try:
                actions = json.loads(actions_path.read_bytes())
            except ValueError:
                gap_runs.add(run)
                continue
            for action in actions if isinstance(actions, list) else []:
                arguments = action.get('arguments') if isinstance(action, dict) else None
                if not isinstance(arguments, dict) or not isinstance(arguments.get('command'), str):
                    continue
                commands += 1
                if S.shell_shape(arguments['command'], arguments.get('stdin')):
                    matched += 1
                else:
                    gap_runs.add(run)
                    first = arguments['command'].split()[:2]
                    key = ' '.join(first)[:40]
                    unmatched[key] = unmatched.get(key, 0) + 1
    return {'runs': len(runs), 'shellCommands': commands, 'safeShapeMatches': matched,
            'coverage': round(matched / commands, 4) if commands else None,
            'runsWithAShellGap': len(gap_runs), 'unmatchedByLeadingWords': dict(sorted(unmatched.items()))}


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(json.dumps(measure([Path(p) for p in sys.argv[1:]]), indent=2))
