"""Release dashboard: deterministic page from fixture inputs, coverage, privacy and the burn-down series."""
import json
import re

import pytest
import release_dashboard as dashboard
import release_decision_record as decision


def issue(repo, number, title, labels, created, state='open'):
    return {'repo': repo, 'number': number, 'title': title, 'state': state, 'labels': sorted(labels),
            'body_sha256': 'body', 'created_at': created, 'updated_at': created, 'family': None}


DATA = {
    'observed_at': '2026-10-04T02:00:00Z', 'candidate_digest': 'not yet cut',
    'issues': [
        issue('honua-sdk-js', 5, 'docs: example', ['priority/P2', 'release/2026.1'], '2026-08-01T00:00:00Z'),
        issue('honua-server', 9, 'fix: already fixed', ['bug', 'release/2026.1'], '2026-07-01T00:00:00Z', state='closed'),
        issue('honua-server', 10, 'fix: crash on empty layer', ['bucket/must-fix-before-cut', 'bug', 'priority/P2', 'release/2026.1'], '2026-09-01T00:00:00Z'),
        issue('honua-server', 11, 'fix(ci): flaky shard', ['release/2026.1'], '2026-09-20T00:00:00Z'),
        issue('honua-server', 12, 'Add a <widget>', ['priority/P1', 'release/2026.1'], '2026-09-30T00:00:00Z'),
        issue('honua-server', 13, 'Later work', ['release/2026.2'], '2026-09-02T00:00:00Z'),
        issue('honua-support', 3, 'evidence: support receipt', ['evidence', 'release/2026.1'], '2026-09-15T00:00:00Z'),
    ],
}
RULES = {
    'exceptions': {}, 'admission_reviews': {},
    'security_findings': [{'id': 'SEC-1', 'repo': 'honua-io/honua-server', 'status': 'open'},
                          {'id': 'SEC-2', 'repo': 'honua-io/honua-server', 'status': 'fixed', 'fixedBy': '7'}],
    'rulings': {decision.SECURITY_REVIEW: {'counts': {'ga_blocker': 2}}},
}
COVERAGE = {
    'observed_at': '2026-10-04T03:00:00Z',
    'repos_scanned': ['honua-sdk-js', 'honua-server'], 'public_repos': ['honua-sdk-js', 'honua-server'],
    'closes': {'honua-server#11': [{'repo': 'honua-server', 'number': 40, 'merge_state': 'CLEAN'}]},
}
SERIES = [
    {'date': '2026-09-11', 'observed_at': '2026-09-11T16:09:45Z', 'open': {'must-fix-before-cut': 9, 'prove-against-candidate': 2, 'post-cut-hardening': 1, '2026.2': 1}},
    {'date': '2026-10-04', 'observed_at': '2026-10-04T02:00:00Z', 'open': {'must-fix-before-cut': 2, 'prove-against-candidate': 1, 'post-cut-hardening': 1, '2026.2': 1}},
]


def page():
    return dashboard.render(DATA, RULES, COVERAGE, SERIES)


def section(html, slug):
    return re.search(rf'<section id="{slug}">.*?</section>', html, re.S).group(0)


def test_fixture_inputs_render_deterministic_counts_and_rows():
    html = page()
    assert html == page()
    # Total, then the four buckets in decision-record order, then the unclassifiable P1.
    assert re.findall(r'<div class="n">(\d+)</div>', html) == ['6', '2', '1', '1', '1', '1']
    assert 'Open tickets in 3 repositories' in html
    assert ('<tr><td class="ticket"><a href="https://github.com/honua-io/honua-server/issues/10">honua-server#10</a></td>'
            '<td class="title">fix: crash on empty layer</td>'
            '<td class="tags"><span class="tag">bug</span><span class="tag">priority/P2</span></td>'
            '<td class="num">33</td><td class="cover"><span class="uncovered">uncovered</span></td></tr>') in html
    by_repo = section(html, 'by-repo')
    assert '<tr class="total"><td>Total</td><td class="num">2</td><td class="num">1</td><td class="num">1</td><td class="num">1</td><td class="num">1</td><td class="num">6</td></tr>' in by_repo
    assert 'honua-server#9' not in html  # closed cohort members stay in the ledger, not on the board


def test_banner_carries_decision_candidate_observed_time_and_open_security_ids_only():
    html = page()
    assert 'Decision: HOLD (2 pre-cut blockers; 1 GA-blocking security findings open; no signed lock)' in html
    assert '<strong>Candidate:</strong> not yet cut' in html
    assert '<strong>Observed:</strong> 2026-10-04T02:00:00Z' in html
    assert '<span class="id">SEC-1</span>' in html and 'SEC-2' not in html
    assert 'honua-security' not in html


def test_bucket_tables_sort_uncovered_first_then_oldest_and_show_merge_state():
    must_fix = section(page(), 'bucket-must-fix-before-cut')
    assert must_fix.index('honua-server#10') < must_fix.index('honua-server#11')
    assert '<a href="https://github.com/honua-io/honua-server/pull/40">#40</a> <span class="state">CLEAN</span>' in must_fix
    grouped = dashboard.bucket_rows(decision.decisions(DATA, RULES, strict=False), {**COVERAGE, 'closes': {}},
                                    dashboard.date(2026, 10, 4))
    assert [r['number'] for r in grouped['must-fix-before-cut']] == [10, 11]  # both uncovered: oldest first
    covered_old = {**COVERAGE, 'closes': {'honua-server#10': COVERAGE['closes']['honua-server#11']}}
    grouped = dashboard.bucket_rows(decision.decisions(DATA, RULES, strict=False), covered_old, dashboard.date(2026, 10, 4))
    assert [r['number'] for r in grouped['must-fix-before-cut']] == [11, 10]  # uncovered beats older


def test_private_or_unread_repositories_are_never_linked_or_called_uncovered():
    prove = section(page(), 'bucket-prove-against-candidate')
    assert '<td class="ticket">honua-support#3</td>' in prove
    assert 'honua-io/honua-support' not in prove
    assert '<span class="not-observed">not observed</span>' in prove
    offline = dashboard.render(DATA, RULES, None, SERIES)
    assert 'href="https://github.com/honua-io/honua-server/issues' not in offline
    assert 'class="uncovered"' not in offline


def test_unclassifiable_ticket_is_shown_unbucketed_not_dropped_and_titles_are_escaped():
    unbucketed = section(page(), 'bucket-unbucketed')
    assert 'Add a &lt;widget&gt;' in unbucketed and 'missing/stale body admission review' in unbucketed
    with pytest.raises(ValueError, match='unclassified'):
        decision.decisions(DATA, RULES)  # the decision record itself stays strict


def test_rows_without_a_creation_time_have_no_age():
    data = {**DATA, 'issues': [{k: v for k, v in i.items() if k != 'created_at'} for i in DATA['issues']]}
    row = re.search(r'<tr><td class="ticket"><a[^>]*>honua-server#10</a>.*?</tr>', dashboard.render(data, RULES, COVERAGE, SERIES)).group(0)
    assert '<td class="num">—</td>' in row


@pytest.mark.parametrize('body,expected', [
    ('Closes #12', {'honua-server#12'}),
    ('fixes: honua-io/honua-sdk-js#5 and resolves https://github.com/honua-io/honua-iac/issues/7', {'honua-sdk-js#5', 'honua-iac#7'}),
    ('Refs #12; closes other-org/repo#3; see honua-server#4', set()),
    ('Resolved #1, Fixed #2', {'honua-server#1', 'honua-server#2'}),
    (None, set()),
])
def test_closing_refs_follow_github_keywords(body, expected):
    assert dashboard.closing_refs(body, 'honua-server') == expected


def test_live_coverage_reads_the_record_repo_list_and_batches_merge_states(monkeypatch):
    repos = [{'name': 'honua-server', 'private': False}, {'name': 'honua-support', 'private': True}]
    prs = {'honua-server': [{'number': 40, 'body': 'Closes #11'}, {'number': 41, 'body': 'Refs #11'}],
           'honua-support': [{'number': 2, 'body': 'Fixes honua-io/honua-server#11\nCloses #3'}]}
    monkeypatch.setattr(decision, 'org_repos', lambda: repos)
    monkeypatch.setattr(decision, 'pages', lambda endpoint: prs[endpoint.split('/')[2]])
    queries = []
    def gh(*args, payload=None):
        queries.append(payload['query'])
        return {'data': {'p0': {'pullRequest': {'mergeStateStatus': 'BLOCKED'}}, 'p1': {'pullRequest': None}}}
    monkeypatch.setattr(decision, 'gh', gh)
    coverage = dashboard.harvest_coverage()
    assert len(queries) == 1
    assert coverage['repos_scanned'] == ['honua-server', 'honua-support']
    assert coverage['public_repos'] == ['honua-server']
    assert coverage['closes'] == {
        'honua-server#11': [{'repo': 'honua-server', 'number': 40, 'merge_state': 'BLOCKED'},
                            {'repo': 'honua-support', 'number': 2, 'merge_state': 'UNKNOWN'}],
        'honua-support#3': [{'repo': 'honua-support', 'number': 2, 'merge_state': 'UNKNOWN'}],
    }


def test_burndown_append_is_idempotent_per_day(tmp_path):
    path = tmp_path / 'burndown.jsonl'
    first = decision.burndown_point(DATA, decision.decisions({**DATA, 'issues': DATA['issues'][:4]}, RULES))
    decision.append_burndown(first, path)
    decision.append_burndown(first, path)
    assert path.read_text().count('\n') == 1
    later = {**first, 'observed_at': '2026-10-04T20:00:00Z', 'open': {**first['open'], 'must-fix-before-cut': 1}}
    decision.append_burndown(later, path)
    assert decision.read_burndown(path) == [later]  # the day's last refresh wins
    before = {**first, 'date': '2026-10-01', 'observed_at': '2026-10-01T00:00:00Z'}
    decision.append_burndown(before, path)
    assert [p['date'] for p in decision.read_burndown(path)] == ['2026-10-01', '2026-10-04']
    assert first['open'] == {'must-fix-before-cut': 2, 'prove-against-candidate': 0, 'post-cut-hardening': 1, '2026.2': 0}


def test_refresh_appends_the_burndown_point(tmp_path, monkeypatch):
    rules = {**RULES, 'exceptions': {'honua-server#12': {'bucket': 'post-cut-hardening', 'reason': 'Reviewed.'}}}
    paths = {}
    for name in ('INPUTS', 'OVERRIDES', 'RECORD', 'LEDGER', 'BURNDOWN'):
        paths[name] = tmp_path / name.lower()
        monkeypatch.setattr(decision, name, paths[name])
    paths['INPUTS'].write_text(json.dumps(DATA))
    paths['OVERRIDES'].write_text(json.dumps(rules))
    monkeypatch.setattr(decision, 'refresh', lambda data: data)
    monkeypatch.setattr(decision.sys, 'argv', ['record', '--refresh'])
    decision.main()
    decision.main()
    assert decision.read_burndown(paths['BURNDOWN']) == [{
        'date': '2026-10-04', 'observed_at': '2026-10-04T02:00:00Z',
        'open': {'must-fix-before-cut': 2, 'prove-against-candidate': 1, 'post-cut-hardening': 2, '2026.2': 1}}]


def test_normalize_records_creation_time_for_ticket_age():
    item = {'repository_url': 'https://api.github.com/repos/honua-io/honua-server', 'number': 1, 'title': 't',
            'state': 'open', 'labels': [], 'body': '', 'created_at': '2026-09-01T00:00:00Z', 'updated_at': '2026-09-02T00:00:00Z'}
    assert decision.normalize(item)['created_at'] == '2026-09-01T00:00:00Z'


def test_burndown_chart_is_to_scale_with_labelled_axes():
    svg = dashboard.burndown_svg([SERIES[0], {**SERIES[1], 'date': '2026-09-12'}, SERIES[1]])
    xs = [float(x) for x in re.findall(r'<circle class="dot s1" cx="([\d.]+)"', svg)]
    # 1 day then 22 days: horizontal gaps are proportional to elapsed days.
    assert (xs[2] - xs[1]) / (xs[1] - xs[0]) == pytest.approx(22, rel=1e-2)
    ys = [float(y) for y in re.findall(r'<circle class="dot s1" cx="[\d.]+" cy="([\d.]+)"', svg)]
    baseline = float(re.search(r'<line class="baseline"[^>]* y1="([\d.]+)"', svg).group(1))
    assert (baseline - ys[0]) / (baseline - ys[2]) == pytest.approx(9 / 2, rel=1e-2)
    assert 'Open tickets</text>' in svg and 'Observed day (UTC)</text>' in svg
    assert '>2026-10-04</text>' in svg and '>Must fix 2</text>' in svg
    assert 'No burn-down points yet' in dashboard.burndown_svg([])
    assert svg.count('<polyline') == 4 and dashboard.burndown_svg(SERIES[:1]).count('<polyline') == 0


def test_page_is_theme_aware_and_phone_safe():
    html = page()
    assert '@media (prefers-color-scheme: dark)' in html
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert html.count('<div class="scroll"><table>') == 7  # every table scrolls inside its card
