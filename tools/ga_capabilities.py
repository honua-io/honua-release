#!/usr/bin/env python3
"""GA capability gate: every GA capability of the candidate server must carry its proof.

The corpus is read from the candidate's own capability matrix
(`docs/gis/data/capability-matrix.v1.json` at the candidate SHA). A capability is GA when it has
a surface of its own, at least one implemented entry, and a status that is not preview,
experimental, internal or deferred. Each GA capability fails the gate when it lacks:

  proving      `provingTestCount` at or above the ledger floor;
  security     a proving test in the candidate's `feature-catalog.json` whose name shows an
               authentication, authorization, isolation or injection check;
  interop      for a protocol-facing capability (a ledger protocol category, a joined CITE suite
               or a client-interop crosswalk), a client-interop result no older than
               `interopMaxAgeDays` at the time of the check;
  cite         a 100% pass rate on every CITE suite the matrix joins;
  evaluation   for an AI capability, a declared evaluation in certification/ga-capabilities.v1.json
               that resolves to a release-promise row or an evaluation file in this repository.

The ledger never lists capabilities as exempt: a capability leaves the gate only by becoming
preview, experimental or internal at the candidate. A missing or malformed input fails closed.

  python tools/ga_capabilities.py                 # producer: receipt for the candidate env
  python tools/ga_capabilities.py --diagnostic    # report only, no receipt (local or PR use)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import source_facts

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / 'certification/ga-capabilities.v1.json'
CONTRACT = ROOT / 'certification/release-promise.v1.json'
MATRIX = 'docs/gis/data/capability-matrix.v1.json'
CATALOG = 'docs/gis/data/feature-catalog.json'
ROW = 'ga-capabilities'
RECEIPT = Path('ga-capabilities.json')
REPORT = Path('ga-capabilities-report.json')
SHOWN_FINDINGS = 200
FULL_PASS_RATE = 100.0
LEDGER_KEYS = frozenset({'schema', 'authority', 'minProvingTests', 'interopMaxAgeDays',
                         'nonGaStatuses', 'protocolCategories', 'aiCategories',
                         'securityTestPattern', 'evaluations'})


class Unavailable(Exception):
    pass


def _positive_int(ledger: dict, key: str) -> int:
    value = ledger.get(key)
    if type(value) is not int or value <= 0:
        raise Unavailable(f'ledger {key} must be a positive integer')
    return value


def _string_list(value, what: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise Unavailable(f'ledger {what} must be a list of strings')
    return value


def load_ledger(path: Path = LEDGER) -> dict:
    try:
        ledger = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise Unavailable(f'cannot read {path.name}: {error}') from error
    if not isinstance(ledger, dict) or ledger.get('schema') != 'honua.ga-capabilities/v1':
        raise Unavailable(f'{path.name} is not a honua.ga-capabilities/v1 ledger')
    if set(ledger) != LEDGER_KEYS:
        raise Unavailable(f'{path.name} keys must be exactly {sorted(LEDGER_KEYS)}')
    _positive_int(ledger, 'minProvingTests')
    _positive_int(ledger, 'interopMaxAgeDays')
    for key in ('nonGaStatuses', 'protocolCategories', 'aiCategories'):
        _string_list(ledger[key], key)
    try:
        ledger['securityTestRegex'] = re.compile(ledger['securityTestPattern'], re.IGNORECASE)
    except (TypeError, re.error) as error:
        raise Unavailable(f'ledger securityTestPattern is invalid: {error}') from error
    evaluations = ledger['evaluations']
    if not isinstance(evaluations, dict):
        raise Unavailable('ledger evaluations must map capability keys to references')
    for key, refs in evaluations.items():
        _string_list(refs, f'evaluations[{key}]')
    return ledger


def promise_rows(path: Path = CONTRACT) -> set[str]:
    return {row['id'] for row in json.loads(path.read_text())['rows']}


def read_json(checkout: Path, relative: str) -> dict:
    try:
        document = json.loads((checkout / relative).read_text())
    except (OSError, ValueError) as error:
        raise Unavailable(f'cannot read {relative} at the candidate: {error}') from error
    if not isinstance(document, dict):
        raise Unavailable(f'{relative} at the candidate is not a JSON object')
    return document


def ga_corpus(matrix: dict, ledger: dict) -> list[dict]:
    entries = matrix.get('capabilities')
    if not isinstance(entries, list):
        raise Unavailable('capability matrix has no capabilities list')
    excluded = set(ledger['nonGaStatuses'])
    corpus = [entry for entry in entries if isinstance(entry, dict) and not entry.get('noSurface')
              and ((entry.get('maturity') or {}).get('implemented') or 0) > 0
              and entry.get('status') not in excluded]
    if not corpus:
        raise Unavailable('capability matrix has no GA capability; refusing a vacuous pass')
    return corpus


def tests_by_capability(catalog: dict) -> dict[str, set[str]]:
    entries = catalog.get('entries')
    if not isinstance(entries, list):
        raise Unavailable('feature catalog has no entries list')
    tests: dict[str, set[str]] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get('capability'), str):
            names = [name for name in entry.get('proving_tests') or [] if isinstance(name, str)]
            tests.setdefault(entry['capability'], set()).update(names)
    return tests


def instant(value) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def proving_gap(entry: dict, ledger: dict) -> str | None:
    count = entry.get('provingTestCount') or 0
    floor = ledger['minProvingTests']
    return None if count >= floor else f'provingTestCount {count} is below the floor {floor}'


def security_tests(entry: dict, tests: dict[str, set[str]], ledger: dict) -> list[str]:
    pattern = ledger['securityTestRegex']
    return sorted(name for name in tests.get(entry['key'], ()) if pattern.search(name))


def protocol_facing(entry: dict, ledger: dict) -> bool:
    return bool(entry.get('category') in ledger['protocolCategories']
                or entry.get('cite') or entry.get('interop'))


def newest_interop(entry: dict) -> dt.datetime | None:
    """Newest green result: the matrix marks a failed or out-of-window envelope `stale`."""
    freshness = [lane.get('freshness') or {} for lane in entry.get('interop') or []
                 if isinstance(lane, dict)]
    dates = [instant(seen.get('runDate')) for seen in freshness if seen.get('state') == 'fresh']
    dates = [date for date in dates if date is not None]
    return max(dates) if dates else None


def interop_gap(entry: dict, ledger: dict, now: dt.datetime) -> str | None:
    if not protocol_facing(entry, ledger):
        return None
    if not entry.get('interop'):
        return 'protocol-facing capability has no client-interop lane'
    newest = newest_interop(entry)
    limit = ledger['interopMaxAgeDays']
    if newest is None:
        return 'no client-interop lane has a passing result'
    if newest > now or now - newest > dt.timedelta(days=limit):
        age = (now - newest).days
        return f'newest client-interop result is {age} days old (limit {limit})'
    return None


def interop_observed_at(entry: dict, ledger: dict) -> str | None:
    newest = newest_interop(entry) if protocol_facing(entry, ledger) else None
    return stamp(newest) if newest else None


def source_observed_at(results: list[dict], now: dt.datetime) -> str:
    """Oldest interop evidence the verdict relies on; admission re-checks its 7-day window."""
    dates = [result['interopObservedAt'] for result in results if result['interopObservedAt']]
    return min(dates, key=instant) if dates else stamp(now)


def cite_gap(entry: dict) -> str | None:
    short = [suite for suite in entry.get('cite') or []
             if not isinstance(suite, dict) or (suite.get('passRate') or 0) < FULL_PASS_RATE]
    if not short:
        return None
    names = ', '.join(f"{suite.get('suite')} {suite.get('passRate')}%" if isinstance(suite, dict)
                      else repr(suite) for suite in short)
    return f'CITE suite below 100%: {names}'


def evaluation_resolves(ref: str, rows: set[str]) -> bool:
    kind, _, target = ref.partition(':')
    if kind == 'promise':
        return target in rows
    if kind == 'path':
        path = (ROOT / target).resolve()
        return target != '' and path.is_relative_to(ROOT) and path.is_file()
    return False


def evaluation_gap(entry: dict, ledger: dict, rows: set[str]) -> str | None:
    if entry.get('category') not in ledger['aiCategories']:
        return None
    refs = ledger['evaluations'].get(entry['key'], [])
    if not refs:
        return 'AI capability declares no evaluation'
    broken = [ref for ref in refs if not evaluation_resolves(ref, rows)]
    return f"evaluation does not resolve: {', '.join(broken)}" if broken else None


def evaluate_capability(entry: dict, context: tuple) -> dict:
    tests, ledger, rows, now = context
    found = security_tests(entry, tests, ledger)
    gaps = {
        'proving': proving_gap(entry, ledger),
        'security': None if found else 'no security test among the proving tests',
        'interop': interop_gap(entry, ledger, now),
        'cite': cite_gap(entry),
        'evaluation': evaluation_gap(entry, ledger, rows),
    }
    return {
        'key': entry.get('key'), 'displayName': entry.get('displayName'),
        'category': entry.get('category'), 'provingTests': entry.get('provingTestCount') or 0,
        'securityTests': len(found), 'protocolFacing': protocol_facing(entry, ledger),
        'interopObservedAt': interop_observed_at(entry, ledger),
        'gaps': {criterion: detail for criterion, detail in gaps.items() if detail},
    }


def evaluate(matrix: dict, catalog: dict, ledger: dict, now: dt.datetime) -> list[dict]:
    context = (tests_by_capability(catalog), ledger, promise_rows(), now)
    corpus = ga_corpus(matrix, ledger)
    if any(not isinstance(entry.get('key'), str) for entry in corpus):
        raise Unavailable('a GA capability has no key')
    return [evaluate_capability(entry, context) for entry in sorted(corpus, key=lambda e: e['key'])]


def totals(results: list[dict]) -> dict:
    criteria = ('proving', 'security', 'interop', 'cite', 'evaluation')
    return {
        'gaCapabilities': len(results),
        'capabilitiesFailing': sum(1 for result in results if result['gaps']),
        **{f'missing{name.capitalize()}': sum(1 for result in results if name in result['gaps'])
           for name in criteria},
    }


def stamp(moment: dt.datetime) -> str:
    return moment.isoformat().replace('+00:00', 'Z')


def findings(results: list[dict]) -> list[str]:
    return [f"{result['key']}: {criterion}: {detail}"
            for result in results for criterion, detail in result['gaps'].items()]


def receipt(candidate, results: list[dict], now: dt.datetime) -> dict:
    counts = totals(results)
    return {
        'schema': 'honua.release-promise-receipt/v1',
        'row': ROW,
        'status': 'PASS' if counts['capabilitiesFailing'] == 0 else 'FAIL',
        'serverSha': candidate.server_sha,
        'imageDigest': candidate.server_digest,
        'platformLockDigest': candidate.platform_lock,
        'observedAt': stamp(now),
        'sourceObservedAt': source_observed_at(results, now),
        'assertions': counts,
        'findings': findings(results)[:SHOWN_FINDINGS],
    }


def summary(results: list[dict], server_sha: str) -> str:
    counts = totals(results)
    lines = [f'## GA capability gate at `{server_sha}`', '',
             f"{counts['capabilitiesFailing']} of {counts['gaCapabilities']} GA capabilities lack proof "
             f"(proving {counts['missingProving']}, security {counts['missingSecurity']}, "
             f"interop {counts['missingInterop']}, CITE {counts['missingCite']}, "
             f"evaluation {counts['missingEvaluation']}).", '',
             '| Capability | Category | Proving | Security | Missing |', '|---|---|---|---|---|']
    for result in results:
        missing = '; '.join(f'{name}: {detail}' for name, detail in result['gaps'].items()) or 'none'
        lines.append(f"| `{result['key']}` | {result['category']} | {result['provingTests']} "
                     f"| {result['securityTests']} | {missing} |")
    return '\n'.join(lines) + '\n'


def head_sha(checkout: Path) -> str:
    try:
        return subprocess.check_output(['git', '-C', str(checkout), 'rev-parse', 'HEAD'],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.CalledProcessError):
        return 'unknown'


def diagnose(checkout: Path, now: dt.datetime) -> tuple[list[dict], None]:
    return evaluate(read_json(checkout, MATRIX), read_json(checkout, CATALOG), load_ledger(), now), None


def certify(checkout: Path, now: dt.datetime) -> tuple[list[dict], dict]:
    candidate = source_facts.candidate_from_environment()
    source_facts.require_candidate_checkout(checkout, candidate)
    results, _ = diagnose(checkout, now)
    return results, receipt(candidate, results, now)


def write_outputs(results: list[dict], document: dict | None, server_sha: str) -> None:
    REPORT.write_text(json.dumps({'serverSha': server_sha, 'totals': totals(results),
                                  'capabilities': results}, indent=2) + '\n')
    if document is not None:
        RECEIPT.write_text(json.dumps(document, indent=2) + '\n')
    text = summary(results, server_sha)
    step_summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if step_summary:
        with open(step_summary, 'a', encoding='utf-8') as handle:
            handle.write(text)
    print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--diagnostic', action='store_true',
                        help='report only; never writes a promise receipt')
    args = parser.parse_args(argv)
    RECEIPT.unlink(missing_ok=True)
    checkout = Path(os.environ.get('SERVER_CHECKOUT', 'server'))
    now = dt.datetime.now(dt.timezone.utc)
    try:
        results, document = (diagnose if args.diagnostic else certify)(checkout, now)
    except (Unavailable, source_facts.FactsUnavailable, OSError, ValueError, KeyError,
            subprocess.CalledProcessError) as error:
        print(f'GA capability gate unavailable: {error}', file=sys.stderr)
        return 1
    write_outputs(results, document, head_sha(checkout))
    return 0 if totals(results)['capabilitiesFailing'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
