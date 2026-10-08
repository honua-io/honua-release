#!/usr/bin/env python3
"""Render the public 2026.1 release dashboard (site/index.html) from the decision-record inputs.

Offline: python3 tools/release_dashboard.py [--coverage coverage.json] [--out site/index.html]
Live:    python3 tools/release_dashboard.py --live [--write-coverage coverage.json]

The ticket set, buckets, repo list and Decision come from release_decision_record (the committed
inputs and overrides); the burn-down series is the one its --refresh appends to. --live adds the
in-flight coverage: the open PRs, in every org repository the token can read, whose body closes a
ticket. The page is public (R30): a private-repository row shows only `repo#n` and the neutral
title `Evidence repo#n`, with no issue title or classifier detail. Other ticket titles appear as
recorded, private repositories are never linked, security findings appear by public SEC-N id only,
and no PR title or body is rendered.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import html
import json
import math
from pathlib import Path
import re
import sys

import release_decision_record as record

OUT = record.ROOT / 'site/index.html'
UNBUCKETED = 'unbucketed'
NAMES = {**record.BUCKETS, UNBUCKETED: 'UNBUCKETED (the rules cannot classify it yet)'}
SHORT = {'must-fix-before-cut': 'Must fix', 'prove-against-candidate': 'Prove',
         'post-cut-hardening': 'Post-cut', '2026.2': '2026.2', UNBUCKETED: 'Unbucketed'}
# Categorical slot per bucket, in fixed order; the chart, legend and totals share it.
SLOT = {bucket: i + 1 for i, bucket in enumerate(record.BUCKETS)}
HIDDEN_LABELS = {'release/2026.1'}
REPO_URL = 'https://github.com/honua-io/{repo}/{kind}/{number}'
RECORD_URL = 'https://github.com/honua-io/honua-release/blob/trunk/docs/2026.1-release-decision-record.md'
# GitHub's closing keywords (honoured only on a PR into the default branch). A ref is `#N` (the PR's own repo), `owner/repo#N` or an issue URL.
CLOSING = re.compile(
    r'\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?):?[ \t]+'
    r'(?:https://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_repo>[\w.-]+)/issues/(?P<url_number>\d+)'
    r'|(?:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+))?#(?P<number>\d+))\b', re.IGNORECASE)
COVERAGE_RANK = {'uncovered': 0, 'not observed': 1, 'covered': 2}


def closing_refs(body, pr_repo):
    """Ticket keys (`repo#N`) in honua-io that a PR body closes."""
    keys = set()
    for m in CLOSING.finditer(body or ''):
        owner = m['url_owner'] or m['owner'] or 'honua-io'
        repo = m['url_repo'] or m['repo'] or pr_repo
        if owner.lower() == 'honua-io':
            keys.add(f"{repo}#{int(m['url_number'] or m['number'])}")
    return keys


def merge_states(prs):
    """mergeStateStatus for each (repo, number), batched into aliased GraphQL reads."""
    states = {}
    for start in range(0, len(prs), 40):
        batch = prs[start:start + 40]
        query = 'query{' + ' '.join(
            f'p{i}: repository(owner:"honua-io",name:{json.dumps(repo)}){{pullRequest(number:{number}){{mergeStateStatus}}}}'
            for i, (repo, number) in enumerate(batch)) + '}'
        data = (record.gh('api', 'graphql', payload={'query': query}) or {}).get('data') or {}
        for i, pr in enumerate(batch):
            node = ((data.get(f'p{i}') or {}).get('pullRequest') or {})
            states[pr] = node.get('mergeStateStatus') or 'UNKNOWN'
    return states


def harvest_coverage():
    """Open PRs that close a ticket, from the same org repository list the record refresh reads.

    GitHub honours closing keywords only on a PR that targets its repository's default branch, so
    a PR into a staging, release or stacked branch covers nothing.
    """
    repos = record.org_repos()
    def fetch(repo):
        prs = record.pages(f"repos/honua-io/{repo['name']}/pulls?state=open&per_page=100")
        return repo['name'], [pr for pr in prs if pr['base']['ref'] == repo['default_branch']]
    closes = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for name, prs in pool.map(fetch, repos):
            for pr in prs:
                for key in closing_refs(pr.get('body'), name):
                    closes.setdefault(key, set()).add((name, pr['number']))
    states = merge_states(sorted({pr for prs in closes.values() for pr in prs}))
    return {
        'observed_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'repos_scanned': sorted(r['name'] for r in repos),
        'public_repos': sorted(r['name'] for r in repos if not r.get('private')),
        'closes': {key: [{'repo': repo, 'number': number, 'merge_state': states[(repo, number)]}
                         for repo, number in sorted(prs)]
                   for key, prs in sorted(closes.items())},
    }


def coverage_of(row, coverage):
    if coverage is None:
        return 'not observed', []
    prs = coverage['closes'].get(record.issue_key(row), [])
    if prs:
        return 'covered', prs
    return ('uncovered' if row['repo'] in coverage['repos_scanned'] else 'not observed'), []


def age_days(row, observed):
    created = row.get('created_at')
    return (observed - date.fromisoformat(created[:10])).days if created else None


def esc(value):
    return html.escape(str(value), quote=True)


def ref(repo, number, kind, public, text=None):
    text = esc(text or f'{repo}#{number}')
    if repo not in public:  # Private repositories (and unknown visibility) are never linked.
        return text
    return f'<a href="{REPO_URL.format(repo=repo, kind=kind, number=number)}">{text}</a>'


def bucket_rows(rows, coverage, observed):
    """Open rows per bucket, uncovered first, then oldest first."""
    grouped = {}
    for row in rows:
        if row['state'] != 'open':
            continue
        state, prs = coverage_of(row, coverage)
        age = age_days(row, observed)
        grouped.setdefault(row['bucket'] or UNBUCKETED, []).append({**row, 'coverage': state, 'prs': prs, 'age': age})
    for items in grouped.values():
        items.sort(key=lambda r: (COVERAGE_RANK[r['coverage']], r['age'] is None, -(r['age'] or 0), r['repo'], r['number']))
    return grouped


def ticket_row(row, public):
    tags = ''.join(f'<span class="tag">{esc(label)}</span>' for label in row['labels']
                   if label not in HIDDEN_LABELS and not label.startswith('bucket/'))
    if row['prs']:
        cover = '<br>'.join(
            ref(pr['repo'], pr['number'], 'pull', public, f"#{pr['number']}" if pr['repo'] == row['repo'] else None)
            + f' <span class="state">{esc(pr["merge_state"])}</span>' for pr in row['prs'])
    else:
        cover = f'<span class="{row["coverage"].replace(" ", "-")}">{esc(row["coverage"])}</span>'
    # Private rows carry the neutral placeholder only. Classifier reasons are detail and stay off
    # the public page, including when the row is unbucketed.
    private = (row['repo'] or '').lower() in record.private_repositories()
    title = record.placeholder_title(row) if private else row['title']
    why = '' if private or row['bucket'] is not None else f'<div class="why">{esc(row["reason"])}</div>'
    return (f'<tr><td class="ticket">{ref(row["repo"], row["number"], "issues", public)}</td>'
            f'<td class="title">{esc(title)}{why}</td><td class="tags">{tags}</td>'
            f'<td class="num">{"—" if row["age"] is None else row["age"]}</td><td class="cover">{cover}</td></tr>')


def nice_step(top, ticks=5):
    """A whole 1/2/5 x 10^k step giving about `ticks` gridlines up to `top`."""
    raw = top / ticks
    if raw <= 1:
        return 1
    power = 10 ** math.floor(math.log10(raw))
    return next(m * power for m in (1, 2, 5, 10) if m * power >= raw)


def spread(positions, gap, low, high):
    """Nudge label y positions apart by at least `gap`, keeping them inside [low, high]."""
    order = sorted(range(len(positions)), key=lambda i: positions[i])
    placed = {}
    previous = None
    for i in order:
        y = positions[i] if previous is None else max(positions[i], previous + gap)
        placed[i] = previous = y
    overflow = (previous or 0) - high
    if overflow > 0:
        for i in placed:
            placed[i] = max(low, placed[i] - overflow)
    return [placed[i] for i in range(len(positions))]


def burndown_svg(series):
    """Open tickets per bucket over observed days; x and y are both to scale."""
    if not series:
        return '<p class="muted">No burn-down points yet; the next decision-record refresh adds the first.</p>'
    width, height, left, right, top, bottom = 720, 320, 56, 132, 16, 52
    plot_w, plot_h = width - left - right, height - top - bottom
    days = [date.fromisoformat(p['date']) for p in series]
    first, span = days[0], max((days[-1] - days[0]).days, 1)
    def x(day):
        return left + (plot_w / 2 if len(days) == 1 else (day - first).days / span * plot_w)
    peak = max((v for p in series for v in p['open'].values()), default=0)
    step = nice_step(peak)
    y_max = max(step, math.ceil(peak / step) * step)
    def y(value):
        return top + plot_h - value / y_max * plot_h
    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" aria-labelledby="bd-title bd-desc">',
           '<title id="bd-title">Open 2026.1 tickets per bucket by observed day</title>',
           '<desc id="bd-desc">' + esc(f'{len(series)} observations from {series[0]["date"]} to {series[-1]["date"]}; latest: '
                                        + ', '.join(f'{NAMES[b]} {series[-1]["open"].get(b, 0)}' for b in record.BUCKETS)) + '</desc>']
    for tick in range(0, y_max + 1, step):
        out.append(f'<line class="grid" x1="{left}" x2="{left + plot_w}" y1="{y(tick):.1f}" y2="{y(tick):.1f}"/>'
                   f'<text class="axis-label" x="{left - 8}" y="{y(tick) + 4:.1f}" text-anchor="end">{tick}</text>')
    out.append(f'<line class="baseline" x1="{left}" x2="{left + plot_w}" y1="{y(0):.1f}" y2="{y(0):.1f}"/>')
    # Label every observed day that fits; the latest day always keeps its label.
    kept = []
    for day in sorted(set(days)):
        if kept and x(day) - x(kept[-1]) < 80:
            if day == days[-1]:
                kept[-1] = day
            continue
        kept.append(day)
    for day in kept:
        out.append(f'<line class="tick" x1="{x(day):.1f}" x2="{x(day):.1f}" y1="{y(0):.1f}" y2="{y(0) + 5:.1f}"/>'
                   f'<text class="axis-label" x="{x(day):.1f}" y="{y(0) + 18:.1f}" text-anchor="middle">{day.isoformat()}</text>')
    out.append(f'<text class="axis-title" x="{left + plot_w / 2:.1f}" y="{height - 6}" text-anchor="middle">Observed day (UTC)</text>'
               f'<text class="axis-title" transform="translate(14 {top + plot_h / 2:.1f}) rotate(-90)" text-anchor="middle">Open tickets</text>')
    ends = []
    for bucket in record.BUCKETS:
        points = [(x(day), y(p['open'][bucket]), p['date'], p['open'][bucket])
                  for day, p in zip(days, series) if bucket in p['open']]
        if not points:
            continue
        slot = SLOT[bucket]
        if len(points) > 1:
            out.append(f'<polyline class="line s{slot}" points="' + ' '.join(f'{px:.1f},{py:.1f}' for px, py, _, _ in points) + '"/>')
        for px, py, day, value in points:
            out.append(f'<g class="point"><title>{esc(f"{day} · {NAMES[bucket]}: {value}")}</title>'
                       f'<circle class="hit" cx="{px:.1f}" cy="{py:.1f}" r="10"/><circle class="dot s{slot}" cx="{px:.1f}" cy="{py:.1f}" r="4"/></g>')
        ends.append((bucket, points[-1]))
    label_ys = spread([py for _, (_, py, _, _) in ends], 15, top + 4, top + plot_h)
    for (bucket, (px, _, _, value)), ly in zip(ends, label_ys):
        out.append(f'<text class="end-label" x="{px + 10:.1f}" y="{ly + 4:.1f}">{esc(SHORT[bucket])} {value}</text>')
    out.append('</svg>')
    return '\n'.join(out)


def burndown_table(series):
    head = ''.join(f'<th class="num">{esc(SHORT[b])}</th>' for b in record.BUCKETS)
    body = ''.join('<tr><td>' + esc(p['date']) + '</td>' + ''.join(f'<td class="num">{p["open"].get(b, "—")}</td>' for b in record.BUCKETS) + '</tr>'
                   for p in series)
    return f'<div class="scroll"><table><thead><tr><th>Observed day</th>{head}</tr></thead><tbody>{body}</tbody></table></div>'


STYLE = """
:root { color-scheme: light; --page:#f9f9f7; --surface:#fcfcfb; --text:#0b0b0b; --text-2:#52514e; --muted:#6b6a65;
  --grid:#e1e0d9; --axis:#c3c2b7; --border:rgba(11,11,11,.10); --link:#1c5cab; --critical:#d03b3b; --good:#0ca30c;
  --s1:#2a78d6; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4; }
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; --page:#0d0d0d; --surface:#1a1a19; --text:#ffffff; --text-2:#c3c2b7; --muted:#9a9890;
    --grid:#2c2c2a; --axis:#383835; --border:rgba(255,255,255,.10); --link:#86b6ef;
    --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--page); color:var(--text); font:15px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif; }
main { max-width:1180px; margin:0 auto; padding:16px; }
h1 { font-size:1.5rem; margin:.5rem 0 1rem; } h2 { font-size:1.15rem; margin:2rem 0 .5rem; }
a { color:var(--link); } .muted, .why, .not-observed { color:var(--text-2); }
section, .banner { background:var(--surface); border:1px solid var(--border); border-radius:8px; padding:12px 16px; margin:12px 0; }
.banner p { margin:.25rem 0; } .decision { font-size:1.1rem; border-left:4px solid var(--critical); padding-left:10px; }
.decision.go { border-left-color:var(--good); }
.tiles { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px; }
.tile { border:1px solid var(--border); border-radius:8px; padding:10px 12px; background:var(--surface); }
.tile .n { font-size:1.8rem; font-weight:600; } .tile .k { color:var(--text-2); font-size:.85rem; }
.swatch { display:inline-block; width:10px; height:10px; border-radius:2px; margin-right:6px; }
.scroll { overflow-x:auto; -webkit-overflow-scrolling:touch; }
table { border-collapse:collapse; width:100%; font-size:.9rem; }
th, td { text-align:left; vertical-align:top; padding:6px 8px; border-bottom:1px solid var(--grid); }
th { color:var(--text-2); font-weight:600; white-space:nowrap; } td.num, th.num { text-align:right; font-variant-numeric:tabular-nums; }
td.ticket, td.cover { white-space:nowrap; } td.title { min-width:16em; } .why { font-size:.8rem; }
.tag { display:inline-block; border:1px solid var(--border); border-radius:10px; padding:0 6px; margin:1px 2px; font-size:.75rem; white-space:nowrap; color:var(--text-2); }
.state { font-size:.75rem; color:var(--text-2); } .uncovered { font-weight:600; }
tr.total td { font-weight:600; border-top:2px solid var(--axis); }
.legend { display:flex; flex-wrap:wrap; gap:4px 16px; margin:4px 0 8px; font-size:.85rem; color:var(--text-2); }
.chart-wrap { overflow-x:auto; } svg.chart { width:100%; min-width:560px; height:auto; display:block; }
.chart text { font-size:12px; } .axis-label { fill:var(--muted); } .axis-title { fill:var(--text-2); } .end-label { fill:var(--text-2); }
.chart .grid { stroke:var(--grid); stroke-width:1; } .chart .baseline, .chart .tick { stroke:var(--axis); stroke-width:1; }
.chart .line { fill:none; stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }
.chart .dot { stroke:var(--surface); stroke-width:2; } .chart .hit { fill:transparent; }
.s1 { stroke:var(--s1); fill:var(--s1); background:var(--s1); } .s2 { stroke:var(--s2); fill:var(--s2); background:var(--s2); }
.s3 { stroke:var(--s3); fill:var(--s3); background:var(--s3); } .s4 { stroke:var(--s4); fill:var(--s4); background:var(--s4); }
.s5 { stroke:var(--s5); fill:var(--s5); background:var(--s5); }
.chart .line.s1, .chart .line.s2, .chart .line.s3, .chart .line.s4 { fill:none; }
footer { color:var(--text-2); font-size:.85rem; margin:24px 0; } .id { white-space:nowrap; }
@media (max-width:600px) { main { padding:8px; } section, .banner { padding:8px 10px; } td.title { min-width:12em; } }
"""


def render(data, rules, coverage=None, series=()):
    rows = record.decisions(data, rules, strict=False)
    _, decision_line = record.decision(data, rows, rules)
    verdict = decision_line.split()[1]
    open_sec = [f['id'] for f in record.security_findings(rules) if f['status'] != 'fixed']
    observed = date.fromisoformat(data['observed_at'][:10])
    grouped = bucket_rows(rows, coverage, observed)
    order = [*record.BUCKETS, *([UNBUCKETED] if UNBUCKETED in grouped else [])]
    public = set(coverage['public_repos']) if coverage else set()
    total = sum(len(grouped.get(b, [])) for b in order)
    wc = data.get('working_candidate')
    candidate = esc(data['candidate_digest'])
    if wc:
        candidate += esc(f' · working candidate: {wc["label"]} ({wc["status"]}; {wc["train_kind"]} train)')
    coverage_note = (f'In-flight coverage observed {esc(coverage["observed_at"])} across {len(coverage["repos_scanned"])} readable repositories; '
                     'a ticket whose repository could not be read shows “not observed”.'
                     if coverage else 'In-flight coverage was not observed for this build.')
    out = ['<!doctype html>', '<html lang="en">', '<head>', '<meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width, initial-scale=1">',
           '<meta name="color-scheme" content="light dark">',
           '<title>Honua 2026.1 release dashboard</title>', f'<style>{STYLE}</style>', '</head>', '<body>', '<main>',
           '<h1>Honua 2026.1 release dashboard</h1>',
           '<div class="banner">',
           f'<p class="decision{" go" if verdict == "GO" else ""}"><strong>{esc(decision_line)}</strong></p>',
           f'<p><strong>Candidate:</strong> {candidate}</p>',
           f'<p><strong>Observed:</strong> {esc(data["observed_at"])}</p>',
           '<p><strong>Open GA-blocking security findings:</strong> ' + (', '.join(f'<span class="id">{esc(i)}</span>' for i in open_sec) if open_sec else 'none') + '</p>',
           f'<p class="muted">{coverage_note}</p>', '</div>',
           '<h2 id="totals">Bucket totals</h2>', '<div class="tiles">',
           f'<div class="tile"><div class="n">{total}</div><div class="k">Open tickets in {len({r["repo"] for b in order for r in grouped.get(b, [])})} repositories</div></div>']
    for bucket in order:
        items = grouped.get(bucket, [])
        swatch = f'<span class="swatch s{SLOT[bucket]}"></span>' if bucket in SLOT else ''
        uncovered = sum(r['coverage'] == 'uncovered' for r in items)
        out.append(f'<div class="tile"><div class="n">{len(items)}</div><div class="k">{swatch}{esc(NAMES[bucket])}'
                   + (f' · {uncovered} uncovered' if coverage else '') + '</div></div>')
    out.append('</div>')
    for bucket in order:
        items = grouped.get(bucket, [])
        slug = re.sub(r'[^a-z0-9]+', '-', bucket.lower()).strip('-')
        out += [f'<section id="bucket-{slug}">',
                f'<h2>{esc(NAMES[bucket])} <span class="muted">({len(items)} open)</span></h2>']
        if not items:
            out += ['<p class="muted">None open.</p>', '</section>']
            continue
        out += ['<div class="scroll"><table>',
                '<thead><tr><th>Ticket</th><th>Title</th><th>Tags</th><th class="num">Age (days)</th><th>In-flight coverage</th></tr></thead>',
                '<tbody>', *(ticket_row(r, public) for r in items), '</tbody></table></div>', '</section>']
    repos = sorted({r['repo'] for b in order for r in grouped.get(b, [])})
    counts = {b: Counter(r['repo'] for r in grouped.get(b, [])) for b in order}
    out += ['<section id="by-repo">', '<h2>By repository</h2>', '<div class="scroll"><table>',
            '<thead><tr><th>Repository</th>' + ''.join(f'<th class="num">{esc(SHORT[b])}</th>' for b in order) + '<th class="num">Total</th></tr></thead>', '<tbody>']
    for repo in repos:
        out.append(f'<tr><td>{esc(repo)}</td>' + ''.join(f'<td class="num">{counts[b][repo]}</td>' for b in order)
                   + f'<td class="num">{sum(counts[b][repo] for b in order)}</td></tr>')
    out += ['<tr class="total"><td>Total</td>' + ''.join(f'<td class="num">{sum(counts[b].values())}</td>' for b in order)
            + f'<td class="num">{total}</td></tr>', '</tbody></table></div>', '</section>']
    out += ['<section id="burndown">', '<h2>Burn-down</h2>',
            '<div class="legend">' + ''.join(f'<span><span class="swatch s{SLOT[b]}"></span>{esc(NAMES[b])}</span>' for b in record.BUCKETS) + '</div>',
            f'<div class="chart-wrap">{burndown_svg(list(series))}</div>',
            '<details><summary>Data table</summary>', burndown_table(list(series)), '</details>', '</section>',
            f'<footer>Generated from the committed 2026.1 decision-record inputs; the buckets and Decision are the '
            f'<a href="{RECORD_URL}">decision record</a>\'s. Age counts days from a ticket\'s creation to the observed day. '
            'Each decision-record refresh adds one burn-down point per UTC day.</footer>',
            '</main>', '</body>', '</html>', '']
    return '\n'.join(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--out', type=Path, default=OUT)
    source = parser.add_mutually_exclusive_group()
    source.add_argument('--live', action='store_true', help='observe in-flight coverage from open PRs')
    source.add_argument('--coverage', type=Path, help='read a coverage file written by --write-coverage')
    parser.add_argument('--write-coverage', type=Path)
    args = parser.parse_args()
    if args.write_coverage and not args.live:
        parser.error('--write-coverage requires --live')
    data = json.loads(record.INPUTS.read_text())
    rules = json.loads(record.OVERRIDES.read_text())
    coverage = harvest_coverage() if args.live else json.loads(args.coverage.read_text()) if args.coverage else None
    if args.write_coverage:
        args.write_coverage.write_text(json.dumps(coverage, indent=2, sort_keys=True) + '\n')
    page = render(data, rules, coverage, record.read_burndown())
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page)
    rows = record.decisions(data, rules, strict=False)
    print(json.dumps(dict(Counter(r['bucket'] or UNBUCKETED for r in rows if r['state'] == 'open')), sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError) as exc:
        sys.exit(str(exc))
