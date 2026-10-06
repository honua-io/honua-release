#!/usr/bin/env python3
"""Regenerate the release decision from labels and reviewed, revision-bound exceptions.

Offline: python3 tools/release_decision_record.py [--check]
Live:    python3 tools/release_decision_record.py --refresh [--apply] [--verify-live]
Gate:    python3 tools/release_decision_record.py --verify-security
Bodies are used only in memory to check admission-review hashes; never persisted.
--apply adds/removes only managed labels and posts the authorized admission comment.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote

from check_vendor_terms import CONFIDENTIAL

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / 'docs/2026.1-release-decision-inputs.json'
OVERRIDES = ROOT / 'docs/2026.1-release-decision-overrides.json'
RECORD = ROOT / 'docs/2026.1-release-decision-record.md'
LEDGER = ROOT / 'docs/2026.1-release-decision-ledger.json'
BURNDOWN = ROOT / 'docs/2026.1-release-burndown.jsonl'
BUCKETS = {
    'must-fix-before-cut': 'MUST FIX BEFORE CANDIDATE CUT',
    'prove-against-candidate': 'MUST PROVE AGAINST THE CANDIDATE BEFORE GA',
    'post-cut-hardening': 'POST-CUT HARDENING',
    '2026.2': '2026.2',
}
LABELS = {'bucket/' + b for b in BUCKETS}
CONTRACT = 'https://github.com/honua-io/honua-flow/blob/trunk/docs/2026.1-quality-contract.md'
RULING = 'https://github.com/honua-io/honua-release/issues/268#issuecomment-5545729823'
# A working-candidate train is described by what produced it, never by a fixed label.
TRAIN_KINDS = {'dry-run', 'scheduled strict', 'dispatched strict'}
SECURITY_REVIEW = 'security-review-2026-10-03'
TRANSIENT = ('error connecting', 'could not resolve host', 'connection reset by peer',
             'tls', 'timeout', 'timed out', 'temporary failure in name resolution')
# R30 detail is whatever the vendor-terms classifier calls confidential; never restate the terms here.
DESKTOP_DETAIL = tuple(pattern for _, pattern in CONFIDENTIAL)
# Same title facts classify() uses. Persisted after redaction so the placeholder title
# cannot move a release blocker between buckets on the next offline regeneration.
_TITLE_EXCLUSION = re.compile(r'(?i)\bepic\b|bug[- ]hunt\b|\bprogram\b|\btriage\b|\bratification\b')
_TITLE_TEST = re.compile(r'(?i)^\s*(\[[a-z-]+\]\s*)?test\b')
_TITLE_CI = re.compile(r'(?i)^\s*(perf|test|chore|fix|feat|ci)\(ci\)|^\s*ci[:(]')
_TITLE_BUG = re.compile(r'(?i)^\s*(\[[a-z-]+\]\s*)?(bug|fix)\b')
_TITLE_SIGNALS = ('excluded', 'test', 'ci', 'bug')


def issue_key(issue):
    return f"{issue['repo']}#{issue['number']}"


def title_signals(title):
    """Non-sensitive classification facts taken from a title."""
    title = title or ''
    present = {
        'excluded': _TITLE_EXCLUSION.search(title) is not None,
        'test': _TITLE_TEST.match(title) is not None,
        'ci': _TITLE_CI.match(title) is not None,
        'bug': _TITLE_BUG.match(title) is not None,
    }
    return [name for name in _TITLE_SIGNALS if present[name]]


def public_issue(issue):
    """R30: retain the issue identity while keeping desktop certification detail private.

    The redacted title is not a classification input. Title-derived signals are
    stored beside it so offline regeneration keeps the live bucket.
    """
    title = issue.get('title') or ''
    if any(pattern.search(title) for pattern in DESKTOP_DETAIL):
        redacted = {key: value for key, value in issue.items() if key != 'title_signals'}
        redacted['title'] = f"Evidence {issue_key(issue)}"
        signals = title_signals(title)
        if signals:
            redacted['title_signals'] = signals
        return redacted
    # Already redacted: keep the stored signals. A live title remains the source of truth.
    if title == f"Evidence {issue_key(issue)}":
        return issue
    if 'title_signals' in issue:
        return {key: value for key, value in issue.items() if key != 'title_signals'}
    return issue


def link(key):
    repo, number = key.split('#')
    return f'[{repo}#{number}](https://github.com/honua-io/{repo}/issues/{number})'


def gh(*args, payload=None):
    """Retry the SAME request; never auth, log request bodies, or print raw responses."""
    cmd = ['gh', *args]
    if payload is not None:
        cmd += ['--input', '-']
    waited = 0
    delays = iter([10, 30, 60, 120, 80])
    while True:
        p = subprocess.run(cmd, input=None if payload is None else json.dumps(payload),
                           text=True, capture_output=True)
        if p.returncode == 0:
            return json.loads(p.stdout) if p.stdout.strip() else None
        error = p.stderr.lower()
        forbidden = '403' in error
        if not forbidden and not any(s in error for s in TRANSIENT):
            status = re.search(r'HTTP (\d+)', p.stderr)
            raise ValueError(f'GitHub request failed ({status[0] if status else "non-transient response"}): {args[1] if len(args)>1 else args[0]}')
        if waited >= 300:
            break
        # 403 waits share the network retry budget; never device-auth or re-auth.
        delay = min(60 if forbidden else next(delays), 300 - waited)
        if forbidden:
            print(f'GitHub 403: sleeping {delay} seconds before retrying the same request.', file=sys.stderr)
        else:
            print(f'Transient GitHub failure; retry budget used {waited}/300 seconds.', file=sys.stderr)
        time.sleep(delay)
        waited += delay
    raise ValueError('GitHub request failed after the full 300-second network retry budget')


def pages(endpoint):
    return [item for page in gh('api', endpoint, '--paginate', '--slurp') for item in page]


def normalize(item, previous=None):
    return {
        'repo': item['repository_url'].rsplit('/', 1)[-1], 'number': item['number'],
        'title': item['title'], 'state': item['state'],
        'labels': sorted(label['name'] for label in item['labels']),
        'body_sha256': hashlib.sha256((item.get('body') or '').encode()).hexdigest(),
        'created_at': item['created_at'], 'updated_at': item['updated_at'],
        'family': (previous or {}).get('family'),
    }


def org_repos():
    """Every org repository the caller can read; the release dashboard scans the same list."""
    return pages('orgs/honua-io/repos?per_page=100')


def refresh(data):
    previous = {issue_key(i): i for i in data['issues']}
    repos = org_repos()
    def fetch(repo):
        return pages(f"repos/honua-io/{repo['name']}/issues?state=open&labels=release%2F2026.1&per_page=100")
    with ThreadPoolExecutor(max_workers=5) as pool:
        fetched = [i for batch in pool.map(fetch, repos) for i in batch if 'pull_request' not in i]
    current = {}
    for item in fetched:
        key = item['repository_url'].rsplit('/', 1)[-1] + '#' + str(item['number'])
        current[key] = normalize(item, previous.get(key))
    # Preserve moved/closed cohort members so removing the release label cannot erase a decision.
    def fetch_retained(key):
        repo, n = key.split('#')
        return key, normalize(gh('api', f'repos/honua-io/{repo}/issues/{n}'), previous[key])
    with ThreadPoolExecutor(max_workers=5) as pool:
        for key, item in pool.map(fetch_retained, sorted(previous.keys() - current.keys())):
            current[key] = item
    with ThreadPoolExecutor(max_workers=5) as pool:
        owners = list(pool.map(fetch_retained_owner, data.get('implementation_owners', [])))
    return {**data, 'observed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'issues': sorted(current.values(), key=lambda i: (i['repo'], i['number'])),
            'implementation_owners': owners}


def fetch_retained_owner(owner):
    repo, n = owner['issue'].split('#')
    item = gh('api', f'repos/honua-io/{repo}/issues/{n}')
    return {**owner, 'state': item['state']}


def _title_facts(issue):
    """Return excluded, is_test, title_is_ci, title_is_bug.

    A persisted Evidence title is read from title_signals, never from the placeholder.
    """
    title = issue.get('title') or ''
    if title == f"Evidence {issue_key(issue)}" and 'title_signals' in issue:
        signals = issue['title_signals']
        if not isinstance(signals, list) or any(signal not in _TITLE_SIGNALS for signal in signals):
            raise ValueError(f"{issue_key(issue)}: unclassified: invalid title_signals")
        found = set(signals)
        return ('excluded' in found, 'test' in found, 'ci' in found, 'bug' in found)
    return (_TITLE_EXCLUSION.search(title) is not None,
            _TITLE_TEST.match(title) is not None,
            _TITLE_CI.match(title) is not None,
            _TITLE_BUG.match(title) is not None)


def classify(issue, rules):
    key = issue_key(issue)
    labels = set(issue['labels'])
    unknown_buckets = {s for s in labels if s.startswith('bucket/')} - LABELS
    if unknown_buckets:
        raise ValueError(f'{key}: unclassified: unknown bucket labels {sorted(unknown_buckets)}')
    priorities = {s for s in labels if s.startswith('priority/')}
    if len(priorities) > 1 or priorities - {'priority/P0', 'priority/P1', 'priority/P2', 'priority/P3'}:
        raise ValueError(f'{key}: unclassified: ambiguous/unknown priority {sorted(priorities)}')
    exception = rules['exceptions'].get(key)
    if exception:
        if exception.get('bucket') not in BUCKETS or not exception.get('reason', '').strip():
            raise ValueError(f'{key}: unclassified: invalid exception or missing reason')
        # A scope/sequence override is always visible, including exceptional P0s.
        return exception['bucket'], exception['reason']
    if 'priority/P0' in labels:
        return 'must-fix-before-cut', 'Every priority/P0 is required before cut (operator 2026-09-05).'
    # Operator ruling 2026-09-11 ("bugs and ci should be pre cut"): a bug or a CI
    # item is must-fix-before-cut whatever its priority; only an explicit
    # release/2026.2 label overrides. Test-expansion items (test(...)),
    # bug-hunt PROGRAM/tracking issues and epics are not bugs and keep their
    # bucket rules. A redacted title carries those facts in title_signals.
    excluded, is_test, title_is_ci, title_is_bug = _title_facts(issue)
    if 'release/2026.2' not in labels and not excluded:
        is_ci = 'area/ci' in labels or title_is_ci
        is_bug = ('bug' in labels
                  or title_is_bug
                  or (any(s.startswith('bug-hunt/') for s in labels) and not is_test))
        if is_ci:
            return 'must-fix-before-cut', 'CI work is pre-cut (operator ruling 2026-09-11).'
        if is_bug:
            return 'must-fix-before-cut', 'Bugs are pre-cut whatever their priority (operator ruling 2026-09-11).'
    review = rules['admission_reviews'].get(key)
    if 'first-release-gate' in labels or 'priority/P1' in labels:
        if not review or review.get('body_sha256') != issue['body_sha256']:
            raise ValueError(f'{key}: unclassified: missing/stale body admission review; add a reasoned override/review')
        if not isinstance(review.get('named_promise'), bool) or not review.get('reason', '').strip():
            raise ValueError(f'{key}: unclassified: invalid admission review')
        if 'first-release-gate' in labels:
            if review['named_promise']:
                return 'must-fix-before-cut', 'First-release gate body names an existing release promise: ' + review['reason']
            return 'post-cut-hardening', 'No named existing release promise; 2026-09-04 admission rule.'
    # Operator ruling 2026-09-10: a release/2026.1 label is authoritative scope.
    # Only an explicit release/2026.2 label sends an issue to 2026.2; feature-type
    # labels no longer demote 2026.1 work (nine tickets were stripped of
    # release/2026.1 by this rule on 2026-09-10 and had to be restored).
    if 'release/2026.2' in labels:
        return '2026.2', 'Explicit release/2026.2 label; outside the 2026.1 supported scope.'
    if labels & {'qualification', 'evidence', 'certification', 'type/qualification', 'type/evidence'}:
        return 'prove-against-candidate', 'Qualification/evidence label requires a receipt bound to the candidate.'
    if 'priority/P1' in labels:
        family = issue.get('family')
        hunt = any(re.fullmatch(r'bug-hunt/(?:.*-)?2026-09-0[34]', s) for s in labels)
        if hunt and family and family['status'] != 'parked':
            return 'must-fix-before-cut', 'P1 in an assigned 2026-09-03/04 hunt fix family.'
        if review['named_promise']:
            return 'must-fix-before-cut', 'P1 body names an existing release promise: ' + review['reason']
        return 'post-cut-hardening', 'Other P1; no named release promise or queued/running fix family.'
    if not priorities or priorities <= {'priority/P2', 'priority/P3'}:
        return 'post-cut-hardening', 'P2/P3/unprioritized default (operator 2026-09-05).'
    raise ValueError(f'{key}: unclassified')


def decisions(data, rules, strict=True):
    """Classify every cohort issue. `strict=False` (the dashboard) keeps an issue the rules
    cannot classify, with bucket None and the classifier's error as its reason."""
    seen = set()
    rows = []
    for issue in data['issues']:
        key = issue_key(issue)
        if key in seen:
            raise ValueError(f'{key}: duplicate issue')
        seen.add(key)
        try:
            bucket, reason = classify(issue, rules)
        except ValueError as exc:
            if strict:
                raise
            bucket, reason = None, str(exc)
        rows.append({**public_issue(issue), 'bucket': bucket, 'reason': reason,
                     'implementation_ticket_closed': issue['state'] == 'closed',
                     'qualified_against_candidate': False,
                     'qualification': 'not yet cut' if data['candidate_digest'] == 'not yet cut' else 'not proven; gate receipts required'})
    if not re.fullmatch(r'sha256:[a-f0-9]{64}|not yet cut', data['candidate_digest']):
        raise ValueError('Invalid candidate digest')
    return rows


def security_findings(rules):
    """Public GA-blocking security rows: `SEC-N` ids and status only; detail stays private.

    Fail closed: the rows must match the ruling's GA-blocker count, and a `fixed` row must
    name the PR that fixed it while an `open` row must not claim one.
    """
    rows = rules.get('security_findings')
    if not isinstance(rows, list) or not rows:
        raise ValueError('security_findings: missing; every GA-blocking finding needs a public row')
    seen = set()
    for row in rows:
        sid = row.get('id', '')
        if not re.fullmatch(r'SEC-[1-9][0-9]*', sid) or sid in seen:
            raise ValueError(f'security_findings: invalid or duplicate id {sid!r}')
        seen.add(sid)
        if not re.fullmatch(r'honua-io/[a-z0-9-]+', row.get('repo', '')):
            raise ValueError(f'{sid}: invalid repo')
        if row.get('status') == 'fixed':
            if not re.fullmatch(r'[1-9][0-9]*', str(row.get('fixedBy', ''))):
                raise ValueError(f'{sid}: a fixed row needs fixedBy: the number of the merged PR')
        elif row.get('status') == 'open':
            if row.get('fixedBy'):
                raise ValueError(f'{sid}: an open row cannot name fixedBy')
        else:
            raise ValueError(f'{sid}: status must be open or fixed')
    expected = rules.get('rulings', {}).get(SECURITY_REVIEW, {}).get('counts', {}).get('ga_blocker')
    if expected != len(rows):
        raise ValueError(f'security_findings: {len(rows)} rows, ruling counts {expected} GA blockers')
    return rows


def signed_lock(data):
    lock = data.get('signed_lock')
    if lock is None:
        return None
    if (not isinstance(lock, dict) or not re.fullmatch(r'2026\.1-rc\.[0-9]+', str(lock.get('tag', '')))
            or not re.fullmatch(r'sha256:[a-f0-9]{64}', str(lock.get('digest', '')))):
        raise ValueError('signed_lock: needs tag 2026.1-rc.N and a sha256 digest')
    return lock


def decision(data, rows, rules):
    """GO only with no pre-cut blocker, every GA-blocking security row fixed and a signed lock."""
    blockers = sum(r['state'] == 'open' and r['bucket'] == 'must-fix-before-cut' for r in rows)
    unfixed = [f['id'] for f in security_findings(rules) if f['status'] != 'fixed']
    lock = signed_lock(data)
    if not blockers and not unfixed and lock:
        return 'GO', f'Decision: GO (signed lock {lock["tag"]})'
    why = []
    if blockers:
        why.append(f'{blockers} pre-cut blockers')
    if unfixed:
        why.append(f'{len(unfixed)} GA-blocking security findings open')
    if not lock:
        why.append('no signed lock')
    return 'HOLD', 'Decision: HOLD (' + '; '.join(why) + ')'


def verify_security(data, rows, rules, record_text):
    """Live gate: each fixed row's PR is merged to its default branch and cites the id;
    any open row keeps the committed record at HOLD."""
    errors = []
    for row in security_findings(rules):
        if row['status'] != 'fixed':
            continue
        pr = gh('api', f"repos/{row['repo']}/pulls/{row['fixedBy']}")
        text = (pr.get('title') or '') + '\n' + (pr.get('body') or '')
        if not pr.get('merged_at'):
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} is not merged")
        elif pr['base']['ref'] != pr['base']['repo']['default_branch']:
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} merged to {pr['base']['ref']}, not the default branch")
        if not re.search(r'\b' + re.escape(row['id']) + r'\b', text):
            errors.append(f"{row['id']}: {row['repo']}#{row['fixedBy']} does not cite {row['id']}")
    verdict, line = decision(data, rows, rules)
    if line not in record_text:
        errors.append(f'record does not carry the computed decision: {line}')
    if any(f['status'] == 'open' for f in security_findings(rules)) and verdict != 'HOLD':
        errors.append('an open GA-blocking security finding requires Decision: HOLD')
    if errors:
        raise ValueError('Security findings gate failed:\n' + '\n'.join(errors))
    return verdict


def label_plan(row, rules):
    labels = set(row['labels'])
    target = 'bucket/' + row['bucket']
    add = {target} - labels
    remove = (labels & LABELS) - {target}
    if row['bucket'] == '2026.2':
        add |= {'release/2026.2'} - labels
        remove |= {'release/2026.1'} & labels
    review = rules['admission_reviews'].get(issue_key(row), {})
    rejected = 'first-release-gate' in labels and review.get('named_promise') is False
    scoped = rules['exceptions'].get(issue_key(row), {}).get('remove_first_release_gate', False)
    if rejected or scoped:
        remove |= {'first-release-gate'} & labels
    return sorted(add), sorted(remove), rejected


def apply_labels(rows, rules):
    # Validate the entire org before the first write; reruns repair partial application.
    for repo in sorted({r['repo'] for r in rows if r['state'] == 'open'}):
        existing = {i['name'] for i in pages(f'repos/honua-io/{repo}/labels?per_page=100')}
        for label in sorted(LABELS | {'release/2026.2'}):
            if label not in existing:
                gh('api', f'repos/honua-io/{repo}/labels', '--method', 'POST',
                   payload={'name': label, 'color': 'b60205' if label.endswith('must-fix-before-cut') else '5319e7',
                            'description': '2026.1 release decision: ' + label.split('/')[-1]})
    for row in rows:
        if row['state'] != 'open':
            continue
        add, remove, rejected = label_plan(row, rules)
        endpoint = f"repos/honua-io/{row['repo']}/issues/{row['number']}"
        if 'first-release-gate' in remove:
            marker = '<!-- release-decision-admission-2026-09-04 -->'
            comments = pages(endpoint + '/comments?per_page=100')
            if not any(marker in (c.get('body') or '') for c in comments):
                reason = 'no named existing release promise in the issue body' if rejected else rules['exceptions'][issue_key(row)]['reason']
                body = f'Removed `first-release-gate`: {reason}; [admission rule, 2026-09-04]({CONTRACT}). {marker}'
                gh('api', endpoint + '/comments', '--method', 'POST', payload={'body': body})
        if add:
            gh('api', endpoint + '/labels', '--method', 'POST', payload={'labels': add})
        for label in remove:
            gh('api', endpoint + '/labels/' + quote(label, safe=''), '--method', 'DELETE')
        if add or remove:
            print(f"Applied {issue_key(row)}: {row['bucket']}", flush=True)


def verify_labels(rows, rules):
    errors = []
    for row in rows:
        if row['state'] == 'open':
            add, remove, _ = label_plan(row, rules)
            if add or remove:
                errors.append(f'{issue_key(row)}: missing {add}, remove {remove}')
    if errors:
        raise ValueError('Live bucket labels disagree:\n' + '\n'.join(errors))


def label_drift(rows):
    """Open rows whose live release label contradicts the recorded bucket.

    A recorded scope/sequence exception outranks a live label, so the bucket is kept
    as decided; the disagreement is reported rather than silently reconciled either way.
    """
    drift = []
    for row in rows:
        if row['state'] != 'open':
            continue
        labels = set(row['labels'])
        if row['bucket'] == '2026.2' and 'release/2026.1' in labels:
            drift.append((issue_key(row), 'recorded 2026.2, labelled release/2026.1'))
        elif row['bucket'] != '2026.2' and 'release/2026.2' in labels:
            drift.append((issue_key(row), f"recorded {row['bucket']}, labelled release/2026.2"))
    return drift


def family_text(row):
    family = row.get('family')
    if not family or family['status'] == 'parked':
        return '**UNOWNED**' if not family else '**UNOWNED** (parked)'
    name = Path(family['packet']).name
    name = re.sub(r'^\d{8}T\d{6}Z-', '', name).removesuffix('.md')
    url = f"https://github.com/honua-io/honua-flow/blob/{family['flow_commit']}/{family['packet']}"
    return f'[{name}]({url}) ({family["status"]})'


def tables(rows):
    active = [r for r in rows if r['state'] == 'open']
    lines = ['| Repo | MUST FIX BEFORE CANDIDATE CUT | MUST PROVE AGAINST THE CANDIDATE BEFORE GA | POST-CUT HARDENING | 2026.2 | Total |',
             '|---|---:|---:|---:|---:|---:|']
    for repo in sorted({r['repo'] for r in rows}):
        counts = Counter(r['bucket'] for r in active if r['repo'] == repo)
        lines.append('| ' + repo + ' | ' + ' | '.join(str(counts[b]) for b in BUCKETS) + f' | {sum(counts.values())} |')
    counts = Counter(r['bucket'] for r in active)
    lines += ['| **Total** | ' + ' | '.join(str(counts[b]) for b in BUCKETS) + f' | **{len(active)}** |', '',
              '| MUST FIX BEFORE CANDIDATE CUT — owner issue | Priority | Owning family packet | Implementation ticket closed | Qualified against candidate |',
              '|---|---|---|---|---|']
    # One row per family keeps the decision short while listing EVERY issue explicitly.
    groups = {}
    for row in active:
        if row['bucket'] == 'must-fix-before-cut':
            p = next((s.removeprefix('priority/') for s in row['labels'] if s.startswith('priority/')), '—')
            groups.setdefault((row['repo'], family_text(row)), []).append(row)
    for (_, family), group in sorted(groups.items()):
        p = '/'.join(sorted({next((s.removeprefix('priority/') for s in r['labels'] if s.startswith('priority/')), '—') for r in group}))
        lines.append('| ' + ', '.join(link(issue_key(r)) if idx == 0 else link(issue_key(r)).replace('[' + issue_key(r) + ']', '[#' + str(r['number']) + ']') for idx,r in enumerate(group)) + f' | {p} | {family} | No | No |')
    return '\n'.join(lines)


def decision_tables(rows):
    counts, blockers = tables(rows).split('\n\n', 1)
    count = sum(r['state']=='open' and r['bucket']=='must-fix-before-cut' for r in rows)
    return counts + f'\n\n<details>\n<summary>{count} pre-cut blockers — every issue, owning packet, implementation and qualification</summary>\n\n' + blockers + '\n\n</details>'


def working_candidate(data):
    # Working candidate pins and the latest train that observed them. Informational only: the digest
    # header stays `not yet cut` and nothing here can mark a row qualified against the candidate.
    wc = data.get('working_candidate')
    if not wc:
        return []
    if wc.get('train_kind') not in TRAIN_KINDS:
        raise ValueError(f'working_candidate.train_kind must be one of {sorted(TRAIN_KINDS)}')
    lines = [f'**Working candidate {wc["label"]} ({wc["status"]}) · {wc["train_kind"]} train [{wc["train"].rsplit("/", 1)[-1]}]({wc["train"]}) · Observed: {wc["observed_at"]}**', '',
             '| Component | Pinned sha | Selection |', '|---|---|---|']
    lines += [f'| {name} | `{sha}` | {why} |' for name, sha, why in wc['pins']]
    lines += ['', '| Train gate | Result | Cause |', '|---|---|---|']
    lines += [f'| {gate} | {status} | {cause} |' for gate, status, cause in wc['gates']]
    # An optional prose note carries what no gate row can: the shape of the train's own input
    # surface (release#338 requires the dry run to show no license input at all).
    if wc.get('note'):
        lines += ['', wc['note']]
    return lines + ['']


def render(data, rows, rules):
    _, decision_line = decision(data, rows, rules)
    findings = security_findings(rules)
    active = [r for r in rows if r['state'] == 'open']
    p0_unowned = [link(issue_key(r)) for r in active if 'priority/P0' in r['labels'] and r['bucket']=='must-fix-before-cut' and (not r.get('family') or r['family']['status']=='parked')]
    p0_activity = Counter(('queued' if r.get('family') and r['family']['status']=='queued' else 'dispatched; not confirmed running' if r.get('family') and r['family']['status'].startswith('dispatched') else 'UNOWNED/parked') for r in active if 'priority/P0' in r['labels'] and r['bucket']=='must-fix-before-cut')
    content = [
        '# 2026.1 release decision record', '',
        (f'**Candidate digest: {data["candidate_digest"]}'
         + (f' · Working server AOT image digest: {data["working_candidate"]["server_image_digest"]}'
            if data.get("working_candidate", {}).get("server_image_digest") else "")
         + f' · {decision_line} · Observed: {data["observed_at"]}**'), '',
        f'[Contract / amendments]({CONTRACT}) · [Canonical rulings]({RULING}) · [Pinned index](https://github.com/honua-io/honua-release/issues/274) · [Every issue + reasons](2026.1-release-decision-ledger.json)', '',
        '2026.1 ships with licensing disabled (`Licensing__Mode=Disabled`): no license file, minting, edition gating or capacity metering. All catalog entitlements are active; serving-unit bands are neither measured nor enforced. Authentication, authorization, safety limits and release maturity remain in force. Whole-catalog GP and COG/Zarr/GeoParquet/PMTiles retain their GA scope; alerting and offline sync remain Preview; multi-tenancy is internal (ruling R29). Licensing hardening, bands, metering, marketplace and the Console licensing page move to 2026.2 (server#4720, iac#190, helm#78, console#384). Operator ruling 2026-09-12 ([release#338](https://github.com/honua-io/honua-release/issues/338)); supersedes the September 5 strict license failure-mode contract for this release.', '',
        *working_candidate(data),
        ('**GA-blocking security findings** (public ids; detail on the private tracker; Decision stays HOLD while any is open): '
         + ', '.join(f'{f["id"]} ' + (f'fixed by [{f["repo"]}#{f["fixedBy"]}](https://github.com/{f["repo"]}/pull/{f["fixedBy"]})' if f['status'] == 'fixed' else 'open')
                     for f in findings) + '.'), '',
        decision_tables(rows), '',
        '**P0 without an assigned fix family:** ' + (', '.join(p0_unowned) or 'None.') + '. P0 fix activity: ' + '; '.join(f'{n} {state}' for state,n in sorted(p0_activity.items())) + '.', '',
        '**Release-label drift (recorded bucket kept, not silently reconciled):** ' + (', '.join(f'{link(key)} — {why}' for key, why in label_drift(rows)) or 'None.'), '',
        '| Supported scope | Denominator / accepted limitations | Accountable owner |', '|---|---|---|',
        f'| GA (qualification pending) | Single-tenant PostGIS core; declared OGC/GeoServices profiles; STAC/Records; whole BuiltInProcessCatalog (no per-op carve-out); COG/Zarr/GeoParquet/PMTiles; local Docker; bounded terminal Admin/SDK/MCP; registry JS/Python/.NET and gRPC .NET via GitHub Packages; focused tested Console. ECS-small x86_64 only with live receipt. AWS Lambda x86_64: GA target, qualification pending; promoted from Preview by the 2026-09-06 contract amendment (ruling A), carrying the full ECS bill — live deploy, serving smoke, upgrade, rollback, destroy on the real serverless substrate; the release cannot be cut with Lambda below that bill. Operating limits: [serverless envelope](2026.1-operating-envelope.md#5-aws-lambda-serverless-supported-target-and-limits). | {link("honua-release#157")}, {link("honua-server#3809")}, {link("geospatial-grpc#88")}, {link("honua-release#129")}, {link("honua-release#282")} |',
        f'| Preview | Studio; realtime; alerting; offline sync; ImageServer + WMTS; EDR/Coverages; NAServer/VersionManagement; Helm/K8s; support application (staffed-manual support required). Security/isolation/integrity floors retained. | {link("honua-release#268")}, {link("honua-server#3859")}, {link("honua-server#3865")}, {link("honua-support#5")} |',
        f'| Internal (ruling R29) | Multi-tenancy runs only on Honua\'s own demo stack, its only deployment. It is not offered for customer deployment and not GA, Preview or Experimental; it has no availability, performance or support claim and is outside every GA denominator. | {link("honua-release#376")} |',
        f'| Excluded from GA | 3D/I3S/terrain/point-cloud/BIM and warehouses remain Experimental, opt-in; this does not demote GA pcloud.translate. ARM64/Fargate, Azure, air-gap, broad deployment variants excluded; broad MCP/OKF Experimental. Branch-versioning expansion and marketplace/billing automation: 2026.2. | {link("honua-server#3249")}, {link("honua-server#3250")}, {link("honua-release#98")} |', '',
        'Operator ruling A (2026-09-13) excludes `activeSubscriptions` and `alertEvaluationsPerSecond` from the 2026.1 capacity envelope. Realtime subscriptions and customer alerting are **Preview**; Preview features carry no capacity promise. The eight GA dimensions are `tenants`, `services`, `layersPerService`, `featuresPerLayer`, `maximumFeaturePayloadBytes`, `concurrentVirtualUsers`, `gpWorkers`, and `gpQueueDepth`. The eight required SLO signals remain availability, error rate, p95/p99 latency, throughput, queue age, saturation, and recovery. See [the capacity ruling](CAPACITY-ENVELOPE-2026.1.md#scope).', '',
        '| Required evidence → consuming §14 gate | Accountable repo + issue | Implementation ticket closed | Qualified against candidate |', '|---|---|---|---|',
    ]
    for owner in data.get('implementation_owners', []):
        content.append(f'| {owner["evidence_gate"]} | {link(owner["issue"])} | {"Yes" if owner["state"]=="closed" else "No"} | No |')
    content += ['',
        '| Remaining decision / limitation | Owner |', '|---|---|',
        f'| Cut blocked by the first bucket; all candidate proofs remain outstanding. Closed machinery is not a receipt. | {link("honua-release#231")}, {link("honua-release#269")} |',
        f'| Whole-catalog GP and all four cloud-native formats retained; exact-candidate SIGKILL/TLS/cleanup follow cut. Explicit exceptions, including superseded P0 labels, carry reasons in the override file. | {link("honua-release#268")}, {link("honua-server#3849")}, {link("honua-devops#183")} |',
        f'| Supported sizes, limits, SLOs and recovery envelope: not qualified until receipts. Support manual; no automated marketplace dependency. Cluster C/D/E rulings not explicitly superseded remain OPEN. | {link("honua-release#235")}, {link("honua-support#5")}, {link("honua-release#268")} |',
        f'| 48-hour burn per promotion candidate with seven consecutive six-hour canaries bound to its lock digest (R19, R20); independent product/quality/security/SRE/support/release approvals; exact-byte promotion and prior-lock rollback. Docker E2E expansion is post-cut hardening. | {link("honua-release#232")}, {link("honua-release#210")}, {link("honua-release#256")} |', '',
        '[Regeneration and label application](2026.1-release-decision-maintenance.md) · [Explicit exceptions / admission reviews](2026.1-release-decision-overrides.json) · [Timestamped inputs](2026.1-release-decision-inputs.json)', '',
    ]
    return '\n'.join(content)


def burndown_point(data, rows):
    """One burn-down observation: open cohort counts per bucket on the observed UTC day."""
    counts = Counter(r['bucket'] for r in rows if r['state'] == 'open')
    return {'date': data['observed_at'][:10], 'observed_at': data['observed_at'],
            'open': {b: counts[b] for b in BUCKETS}}


def read_burndown(path=None):
    path = path or BURNDOWN
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_burndown(point, path=None):
    """Idempotent per day: a later refresh on the same UTC day replaces that day's point."""
    path = path or BURNDOWN
    series = {p['date']: p for p in read_burndown(path)}
    series[point['date']] = point
    path.write_text(''.join(json.dumps(series[d], sort_keys=True) + '\n' for d in sorted(series)))


def compact_snapshot(data):
    metadata = {k:v for k,v in data.items() if k != 'issues'}
    prefix = json.dumps(metadata, indent=2).rstrip()[:-1].rstrip()
    return prefix + ',\n  \"issues\": [\n' + ',\n'.join('    ' + json.dumps(r, ensure_ascii=False) for r in data['issues']) + '\n  ]\n}\n'


def public_snapshot(data):
    """Return the publishable inventory with sensitive titles reduced to identifiers.

    Title-derived classification signals are kept, so a later --check cannot
    reclassify a blocker from the placeholder title.
    """
    return {**data, 'issues': [public_issue(issue) for issue in data['issues']]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify-live', action='store_true')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--tables', type=Path)
    parser.add_argument('--verify-security', action='store_true')
    args = parser.parse_args()
    if (args.apply or args.verify_live) and not args.refresh:
        parser.error('--apply/--verify-live require --refresh')
    if args.check and (args.refresh or args.apply):
        parser.error('--check is offline and cannot mutate inputs')
    if args.verify_security and (args.refresh or args.apply):
        parser.error('--verify-security reads the committed record and cannot mutate inputs')
    data = json.loads(INPUTS.read_text())
    rules = json.loads(OVERRIDES.read_text())
    if args.refresh:
        data = refresh(data)
    rows = decisions(data, rules)
    if args.verify_security:
        verdict = verify_security(data, rows, rules, RECORD.read_text())
        print(f'Security findings gate passed; {verdict}.')
        return
    if args.apply:
        # Keep newly discovered cohort members before removing any release label.
        # An interrupted write pass must remain resumable from this same inventory.
        INPUTS.write_text(compact_snapshot(public_snapshot(data)))
        apply_labels(rows, rules)
        data = refresh(data)
        rows = decisions(data, rules)
        verify_labels(rows, rules)
    elif args.verify_live or args.check:
        verify_labels(rows, rules)
    artifacts = {RECORD: render(data, rows, rules), LEDGER: compact_snapshot({'observed_at':data['observed_at'], 'candidate_digest':data['candidate_digest'], 'issues':rows})}
    if args.refresh:
        artifacts[INPUTS] = compact_snapshot(public_snapshot(data))
    for path, text in artifacts.items():
        if args.check:
            if not path.exists() or path.read_text() != text:
                raise ValueError(f'{path.name}: regeneration drift')
        else:
            path.write_text(text)
    if args.refresh:
        # The published dashboard draws this series; every refresh adds (or replaces) its day.
        append_burndown(burndown_point(data, rows))
    if args.tables:
        args.tables.write_text(tables(rows) + '\n')
    print(json.dumps(dict(Counter(r['bucket'] for r in rows if r['state']=='open')), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as exc:
        sys.exit(str(exc))
