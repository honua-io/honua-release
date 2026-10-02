"""Behaviour of the scheduled strict train and the producers it drives itself.

The workflow `run:` scripts are executed here against a fake `gh` that answers the way the
GitHub API does (a commit SHA as a dispatch ref is 422 "No ref found"), job conditions are
evaluated with GitHub's expression rules, and candidate-input runs against a local origin.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import textwrap

import pytest
import yaml

import nightly_receipts as receipts

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
OTHER = 'b' * 40
SERVER = '5' * 40
SNAPSHOT = 'c' * 40
REVIEWED = 'd' * 40
NEWER = 'e' * 40
IMAGE = 'ghcr.io/honua-io/honua-server@sha256:' + '1' * 64


def workflow(name='nightly-certification.yml'):
    document = yaml.safe_load((ROOT / '.github/workflows' / name).read_text())
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    document['triggers'] = document.get('on', document.get(True))
    return document


def step(job, name_prefix):
    return next(s for s in job['steps'] if s.get('name', '').startswith(name_prefix))


# ── GitHub expression evaluation (the subset these job conditions use) ──────────────────────

TOKEN = re.compile(r"\s*(?:('(?:[^']|'')*')|(&&|\|\||==|!=|!|\(|\))|([A-Za-z_][A-Za-z0-9_.-]*))")
STATUS_FUNCTIONS = ('success', 'failure', 'cancelled', 'always')


def evaluate(expression, context, *, needs_ok=True, cancelled=False):
    """Evaluate a job `if:`. Without a status function GitHub prepends success()."""
    text = str(expression).strip()
    if text.startswith('${{') and text.endswith('}}'):
        text = text[3:-2]
    tokens, position = [], 0
    while position < len(text.rstrip()):
        match = TOKEN.match(text, position)
        assert match, f'cannot tokenize {text[position:]!r}'
        tokens.append(match.group(1) or match.group(2) or match.group(3))
        position = match.end()
    functions = {'success': lambda: needs_ok and not cancelled, 'failure': lambda: not needs_ok,
                 'cancelled': lambda: cancelled, 'always': lambda: True}

    def parse_or(i):
        value, i = parse_and(i)
        while i < len(tokens) and tokens[i] == '||':
            right, i = parse_and(i + 1)
            value = value or right
        return value, i

    def parse_and(i):
        value, i = parse_eq(i)
        while i < len(tokens) and tokens[i] == '&&':
            right, i = parse_eq(i + 1)
            value = value and right
        return value, i

    def parse_eq(i):
        value, i = parse_unary(i)
        while i < len(tokens) and tokens[i] in ('==', '!='):
            operator = tokens[i]
            right, i = parse_unary(i + 1)
            same = str(value).lower() == str(right).lower()
            value = same if operator == '==' else not same
        return value, i

    def parse_unary(i):
        if tokens[i] == '!':
            value, i = parse_unary(i + 1)
            return not value, i
        return parse_primary(i)

    def parse_primary(i):
        token = tokens[i]
        if token == '(':
            value, i = parse_or(i + 1)
            assert tokens[i] == ')'
            return value, i + 1
        if token.startswith("'"):
            return token[1:-1].replace("''", "'"), i + 1
        if i + 1 < len(tokens) and tokens[i + 1] == '(':
            assert tokens[i + 2] == ')'
            return functions[token](), i + 3
        if token in ('true', 'false'):
            return token == 'true', i + 1
        return context.get(token, ''), i + 1

    value, end = parse_or(0)
    assert end == len(tokens), tokens[end:]
    if not any(f'{name}(' in text.replace(' ', '') for name in STATUS_FUNCTIONS):
        value = functions['success']() and value
    return bool(value)


def test_resolve_and_mint_run_only_on_trunk():
    jobs = workflow()['jobs']
    for ref, expected in (('refs/heads/trunk', True), ('refs/heads/release-386-nightly-train', False),
                          ('refs/heads/main', False), ('refs/tags/2026.1-rc.3', False)):
        assert evaluate(jobs['resolve']['if'], {'github.ref': ref}) is expected, ref
        assert evaluate(jobs['mint']['if'], {'github.ref': ref, 'needs.train.result': 'success'}) is expected, ref
    # capacity, dr and train need resolve, so a branch dispatch reaches none of them.
    for name in ('capacity', 'dr', 'train'):
        assert 'resolve' in (jobs[name]['needs'] if isinstance(jobs[name]['needs'], list) else [jobs[name]['needs']])


def test_mint_needs_a_successful_train_and_a_live_night():
    condition = workflow()['jobs']['mint']['if']
    trunk = {'github.ref': 'refs/heads/trunk'}
    assert evaluate(condition, {**trunk, 'needs.train.result': 'success'})
    for result in ('failure', 'skipped', 'cancelled', ''):
        assert not evaluate(condition, {**trunk, 'needs.train.result': result}, needs_ok=False)
    assert not evaluate(condition, {**trunk, 'needs.train.result': 'success'}, cancelled=True)


def test_train_judges_a_published_receipt_but_never_runs_for_a_cancelled_night():
    condition = workflow()['jobs']['train']['if']
    ready = {'needs.resolve.result': 'success', 'needs.capacity.outputs.receipt_url': 'https://raw/x',
             'needs.dr.outputs.receipt_url': 'https://raw/y'}
    assert evaluate(condition, ready)
    # A producer that failed after publishing its receipt is judged by the strict train.
    assert evaluate(condition, ready, needs_ok=False)
    # The previous `always()` ran the train for a cancelled night.
    assert not evaluate(condition, ready, cancelled=True)
    assert not evaluate(condition, {**ready, 'needs.resolve.result': 'skipped'}, needs_ok=False)
    assert not evaluate(condition, {**ready, 'needs.capacity.outputs.receipt_url': ''}, needs_ok=False)
    assert not evaluate(condition, {**ready, 'needs.dr.outputs.receipt_url': ''}, needs_ok=False)


def test_nightly_is_scheduled_and_takes_no_hand_supplied_input():
    nightly = workflow()
    triggers = nightly['triggers']
    assert triggers['schedule'] == [{'cron': '15 11 * * *'}]
    assert not (triggers['workflow_dispatch'] or {}).get('inputs')
    assert 'workflow_call' not in triggers
    assert nightly['concurrency']['cancel-in-progress'] is False


def test_write_scopes_live_only_on_the_jobs_that_use_them():
    nightly = workflow()
    assert nightly['permissions'] == {'contents': 'read'}
    jobs = nightly['jobs']
    writes = {name: sorted(k for k, v in (job.get('permissions') or {}).items() if v == 'write')
              for name, job in jobs.items()}
    assert writes == {
        'resolve': ['contents'],                 # refs/nightly-candidates/<sha>
        'capacity': [],                          # cross-repo work uses RELEASE_GH_TOKEN
        'dr': ['actions'],                       # dispatches the drill in this repository
        'train': ['actions', 'attestations', 'id-token'],  # release-train's own declared scopes
        'mint': ['contents', 'id-token'],        # lock tag + keyless signature
    }
    train = workflow('release-train.yml')
    assert jobs['train']['permissions'] == train['permissions']


def run_blocks(document):
    jobs = document.get('jobs') or {'composite': {'steps': document['runs']['steps']}}
    for name, job in jobs.items():
        for item in job.get('steps') or []:
            if 'run' in item:
                yield name, item.get('name', ''), item['run']


@pytest.mark.parametrize('path', ['.github/workflows/nightly-certification.yml',
                                  '.github/workflows/release-train.yml',
                                  '.github/workflows/gate-journey.yml',
                                  '.github/actions/candidate-input/action.yml'])
def test_no_expression_is_interpolated_into_a_shell_script(path):
    document = yaml.safe_load((ROOT / path).read_text())
    found = [(job, name, expr) for job, name, script in run_blocks(document)
             for expr in re.findall(r'\$\{\{.*?\}\}', script)]
    assert found == [], 'pass inputs, outputs and contexts through env:'


def test_label_with_shell_metacharacters_reaches_the_lock_as_data(tmp_path):
    freeze = workflow('release-train.yml')['jobs']['freeze']
    nightly_lock = step(freeze, 'Generate the unsigned nightly qualification lock')
    assert nightly_lock['env']['PLATFORM_LABEL'] == '${{ inputs.platform_label }}'
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'argv.json'
    shim = bin_dir / 'python'
    shim.write_text(f'#!{sys.executable}\nimport json,sys\n'
                    f'open({str(log)!r},"a").write(json.dumps(sys.argv[1:])+"\\n")\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    marker = tmp_path / 'pwned'
    label = f"x'; touch {marker}; echo '"
    env = {'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'PLATFORM_LABEL': label}
    subprocess.run(['bash', '-c', nightly_lock['run']], cwd=tmp_path, env=env, check=True)
    assert not marker.exists()
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls[-1][calls[-1].index('--label') + 1] == label


# ── release-train: call-only nightly inputs and the real journey gate ───────────────────────

def test_nightly_and_candidate_ref_are_call_only_inputs():
    train = workflow('release-train.yml')
    dispatch = train['triggers']['workflow_dispatch']['inputs']
    called = train['triggers']['workflow_call']['inputs']
    assert 'nightly' not in dispatch and 'candidate_ref' not in dispatch
    assert called['nightly'] == {'required': False, 'type': 'boolean', 'default': False}
    assert called['candidate_ref'] == {'required': False, 'type': 'string', 'default': ''}
    for inputs in (dispatch, called):
        assert inputs['capacity_receipt_url']['required'] is True
        assert inputs['dr_receipt_url']['required'] is True
    # A manual dispatch therefore always takes the signed frozen-lock binding.
    bind = step(train['jobs']['freeze'], 'Bind the complete atomic platform lock')
    assert evaluate(bind['if'], {'inputs.dry_run': False})


def test_the_strict_train_runs_the_real_journey_gate():
    train = workflow('release-train.yml')
    jobs = train['jobs']
    assert 'gate_journey_todo' not in jobs
    journey = jobs['gate_journey']
    assert journey['uses'] == './.github/workflows/gate-journey.yml'
    assert journey['with'] == {
        'receipts_run_id': '${{ github.run_id }}',
        'receipts_run_attempt': '${{ github.run_attempt }}',
        'candidate_digest': '${{ needs.freeze.outputs.manifest_sha256 }}',
        'candidate_bundle': True,
    }
    assert set(journey['needs']) == {'freeze', 'gate_cloud_parity'}
    # Cloud red still lets the journey checker judge GA receipts; a skipped freeze does not.
    assert evaluate(journey['if'], {'needs.freeze.result': 'success'}, needs_ok=False)
    assert not evaluate(journey['if'], {'needs.freeze.result': 'skipped'}, needs_ok=False)
    assert not evaluate(journey['if'], {'needs.freeze.result': 'success'}, cancelled=True)
    assert jobs['freeze']['outputs']['manifest_sha256'] == '${{ steps.candidate.outputs.manifest_sha256 }}'
    assert 'gate_journey' in jobs['report']['needs']
    assemble = step(jobs['report'], 'Assemble platform gate-report.json')
    assert assemble['env']['S_JOURNEY'] == \
        '${{ needs.gate_journey.outputs.overall_status || needs.gate_journey.result }}'
    assert 'journey|$S_JOURNEY' in assemble['run']


@pytest.mark.parametrize('override,accepted', [
    ({}, True),
    ({'event': 'workflow_dispatch'}, True),
    ({'head_branch': 'release-386-nightly-train'}, False),
    ({'event': 'pull_request'}, False),
    ({'path': '.github/workflows/nightly-certification-copy.yml'}, False),
])
def test_journey_gate_trusts_receipts_from_the_trunk_nightly_run(override, accepted):
    if shutil.which('jq') is None:
        pytest.fail('jq is required to evaluate the journey producer predicate')
    gate = workflow('gate-journey.yml')
    check = step(gate['jobs']['journey'], 'Verify the receipt producer is a trunk workflow run')
    query = check['run'].split('jq -e', 1)[1].split("'", 2)[1]
    run = {'id': 77, 'run_attempt': 1, 'head_branch': 'trunk', 'event': 'schedule',
           'path': '.github/workflows/nightly-certification.yml', **override}
    result = subprocess.run(['jq', '-e', '--arg', 'id', '77', '--arg', 'attempt', '1', query],
                            input=json.dumps(run), text=True, capture_output=True)
    assert (result.returncode == 0) is accepted


# ── producers: a fake GitHub that refuses SHA refs exactly as the API does ──────────────────

FAKE_GH = r'''
import json, os, sys
state_path = os.environ['FAKE_GH_STATE']
state = json.load(open(state_path))
args = sys.argv[1:]
state['argv'].append(args)

def save():
    json.dump(state, open(state_path, 'w'))

def fail(status, message):
    save()
    print(json.dumps({'message': message, 'status': str(status)}))
    print(f'gh: {message} (HTTP {status})', file=sys.stderr)
    sys.exit(1)

def resolve(repo, ref):
    refs = state['refs'].get(repo, {})
    for candidate in (ref, 'refs/heads/' + ref, 'refs/tags/' + ref):
        if candidate in refs:
            return candidate, refs[candidate]
    return None, None

if args[0] == 'workflow' and args[1] == 'run':
    repo, ref = args[args.index('--repo') + 1], args[args.index('--ref') + 1]
    if resolve(repo, ref)[0] is None:
        save(); print(f'could not create workflow dispatch event: HTTP 422: No ref found for: {ref}', file=sys.stderr); sys.exit(1)
    save(); sys.exit(0)
if args[0] == 'run' and args[1] == 'view':
    run = state['runs'][args[2]]
    save(); print(run['run_attempt']); sys.exit(0)
if args[0] == 'run' and args[1] == 'download':
    run_id, name, directory = args[2], args[args.index('--name') + 1], args[args.index('--dir') + 1]
    if name != f"dr-receipt-location-{run_id}-{state['runs'][run_id]['run_attempt']}":
        save(); print('no artifact matches', file=sys.stderr); sys.exit(1)
    os.makedirs(directory, exist_ok=True)
    json.dump({'receipt_url': state['dr_receipt_url']}, open(os.path.join(directory, 'receipt-location.json'), 'w'))
    save(); sys.exit(0)
assert args[0] == 'api', args
method = args[args.index('--method') + 1] if '--method' in args else 'GET'
path = args[args.index('--method') + 2] if '--method' in args else args[1]
body = json.loads(sys.stdin.read()) if '--input' in args else None
parts = path.split('?')[0].split('/')
repo = '/'.join(parts[1:3])
if method == 'POST' and parts[3:5] == ['git', 'refs']:
    refs = state['refs'].setdefault(repo, {})
    if body['ref'] in refs:
        fail(422, 'Reference already exists')
    refs[body['ref']] = body['sha']
    save(); print(json.dumps({'ref': body['ref'], 'object': {'type': 'commit', 'sha': body['sha']}})); sys.exit(0)
if method == 'GET' and parts[3:5] == ['git', 'ref']:
    ref = 'refs/' + '/'.join(parts[5:])
    sha = state['refs'].get(repo, {}).get(ref)
    if sha is None:
        fail(404, 'Not Found')
    save(); print(json.dumps({'ref': ref, 'object': {'type': 'commit', 'sha': sha}})); sys.exit(0)
if method == 'POST' and parts[-1] == 'dispatches':
    workflow = parts[-2]
    ref, sha = resolve(repo, body['ref'])
    if ref is None:
        fail(422, f"No ref found for: {body['ref']}")
    state['next_run'] += 1
    run_id = state['next_run']
    state['dispatches'].append({'repo': repo, 'workflow': workflow, **body})
    run = {'id': run_id, 'event': 'workflow_dispatch', 'path': f'.github/workflows/{workflow}',
           'head_sha': sha, 'head_branch': ref.split('/', 2)[2], 'status': 'completed',
           'conclusion': state['conclusion'], 'run_attempt': 1}
    state['runs'][str(run_id)] = run
    if workflow == 'capacity-soak-candidate.yml' and state.get('publish', True):
        candidate = body['inputs']['candidate_sha']
        state['soak'].append({'sha': state['soak_commit'], 'path': f'capacity/{candidate}-{run_id}-1.zip',
                              'commit': {'message': f'capacity evidence for {candidate} (run {run_id}/1)'}})
    save()
    print(json.dumps({'workflow_run_id': run_id} if body.get('return_run_details') else {}))
    sys.exit(0)
if method == 'GET' and parts[3:5] == ['actions', 'runs']:
    run = state['runs'].get(parts[5])
    if run is None:
        fail(404, 'Not Found')
    save(); print(json.dumps(run)); sys.exit(0)
if method == 'GET' and parts[3] == 'compare':
    base, head = parts[4].split('...')
    status = 'identical' if base == head else state['compare'].get(f'{base}...{head}', 'diverged')
    save(); print(json.dumps({'status': status})); sys.exit(0)
if method == 'GET' and parts[3] == 'commits':
    query = dict(item.split('=', 1) for item in path.split('?', 1)[1].split('&'))
    rows = [row for row in state['soak'] if row['path'] == query.get('path')] if query.get('sha') == 'soak-receipts' else []
    save(); print(json.dumps([{'sha': r['sha'], 'commit': r['commit']} for r in rows])); sys.exit(0)
fail(404, f'unrouted {method} {path}')
'''


@pytest.fixture
def github(tmp_path):
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name, body in (('gh', FAKE_GH), ('python', 'import runpy, sys\nsys.argv = sys.argv[1:]\n'
                                                  'runpy.run_path(sys.argv[0], run_name="__main__")\n')):
        path = bin_dir / name
        path.write_text(f'#!{sys.executable}\n{body}')
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    state = {'argv': [], 'refs': {'honua-io/honua-server': {'refs/heads/trunk': NEWER},
                                  'honua-io/honua-release': {'refs/heads/trunk': REVIEWED}},
             'runs': {}, 'dispatches': [], 'next_run': 9000, 'conclusion': 'success', 'compare': {},
             'soak': [], 'soak_commit': '7' * 40,
             'dr_receipt_url': 'https://raw.githubusercontent.com/honua-io/honua-evidence/' + '8' * 40 + '/r.json'}

    class GitHub:
        def __init__(self):
            self.path = tmp_path / 'gh-state.json'
            self.save(state)

        def load(self):
            return json.loads(self.path.read_text())

        def save(self, value):
            self.path.write_text(json.dumps(value))

        def update(self, **values):
            current = self.load()
            current.update(values)
            self.save(current)

        def run_step(self, job, name, env):
            script = step(workflow()['jobs'][job], name)['run']
            output = tmp_path / f'{job}-output.txt'
            output.write_text('')
            work = tmp_path / f'{job}-work'
            work.mkdir(exist_ok=True)
            shutil.copytree(ROOT / 'tools', work / 'tools', dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('__pycache__'))
            result = subprocess.run(['bash', '-c', script], cwd=work, capture_output=True, text=True, env={
                'PATH': f'{bin_dir}:{os.environ["PATH"]}', 'FAKE_GH_STATE': str(self.path),
                'GITHUB_OUTPUT': str(output), 'GITHUB_REPOSITORY': 'honua-io/honua-release', **env})
            outputs = dict(line.split('=', 1) for line in output.read_text().splitlines() if '=' in line)
            return result, outputs

    return GitHub()


CAPACITY_ENV = {'SERVER_SHA': SERVER, 'SERVER_IMAGE': IMAGE, 'CANDIDATE_REF': SNAPSHOT, 'GH_TOKEN': 'x'}
DR_ENV = {'CANDIDATE_REF': SNAPSHOT, 'REVIEWED_SHA': REVIEWED, 'GH_TOKEN': 'x'}


def test_the_api_refuses_a_commit_sha_as_a_dispatch_ref(github):
    """Fidelity of the fake: the previous `gh workflow run --ref "$SERVER_SHA"` is a 422."""
    env = {**os.environ, 'FAKE_GH_STATE': str(github.path)}
    gh = [sys.executable, '-c', FAKE_GH]
    refused = subprocess.run([*gh, 'workflow', 'run', 'capacity-soak-candidate.yml',
                              '--repo', 'honua-io/honua-server', '--ref', SERVER], env=env,
                             capture_output=True, text=True)
    assert refused.returncode == 1 and f'HTTP 422: No ref found for: {SERVER}' in refused.stderr
    accepted = subprocess.run([*gh, 'workflow', 'run', 'dr-drill-local-docker.yml',
                               '--repo', 'honua-io/honua-release', '--ref', 'trunk'], env=env,
                              capture_output=True, text=True)
    assert accepted.returncode == 0


def test_capacity_producer_runs_on_a_tag_at_the_exact_server_commit(github):
    result, outputs = github.run_step('capacity', 'Dispatch the candidate capacity soak', CAPACITY_ENV)
    assert result.returncode == 0, result.stderr
    state = github.load()
    [dispatched] = state['dispatches']
    assert dispatched['repo'] == 'honua-io/honua-server'
    assert dispatched['workflow'] == 'capacity-soak-candidate.yml'
    assert dispatched['ref'] == f'refs/tags/nightly-candidate/{SERVER}'
    assert dispatched['inputs'] == {'candidate_sha': SERVER, 'candidate_image': IMAGE,
                                    'lock_ref': SNAPSHOT, 'publish': 'true'}
    assert state['refs']['honua-io/honua-server'][f'refs/tags/nightly-candidate/{SERVER}'] == SERVER
    run = state['runs'][str(state['next_run'])]
    assert run['head_sha'] == SERVER  # the producer's workflow source is the candidate commit
    assert outputs['receipt_url'] == (f'https://raw.githubusercontent.com/honua-io/honua-server/{"7" * 40}/'
                                      f'capacity/{SERVER}-{run["id"]}-1.zip')


def test_capacity_rerun_reuses_the_tag_but_refuses_one_that_moved(github):
    assert github.run_step('capacity', 'Dispatch the candidate capacity soak', CAPACITY_ENV)[0].returncode == 0
    state = github.load()
    state['refs']['honua-io/honua-server'][f'refs/tags/nightly-candidate/{SERVER}'] = OTHER
    github.save(state)
    result, outputs = github.run_step('capacity', 'Dispatch the candidate capacity soak', CAPACITY_ENV)
    assert result.returncode != 0 and 'does not point at commit' in result.stderr
    assert len(github.load()['dispatches']) == 1 and 'receipt_url' not in outputs


def test_failed_capacity_producer_reports_its_receipt_then_fails(github):
    github.update(conclusion='failure')
    result, outputs = github.run_step('capacity', 'Dispatch the candidate capacity soak', CAPACITY_ENV)
    assert result.returncode == 1 and 'did not succeed' in result.stdout
    assert outputs['receipt_url'].endswith('-1.zip')


def test_capacity_producer_that_published_nothing_yields_no_receipt(github):
    github.update(publish=False, conclusion='failure')
    result, outputs = github.run_step('capacity', 'Dispatch the candidate capacity soak', CAPACITY_ENV)
    assert result.returncode != 0 and 'found 0' in result.stderr
    assert 'receipt_url' not in outputs


def test_dr_drill_runs_on_trunk_after_trunk_moved_past_the_nightly(github):
    state = github.load()
    state['refs']['honua-io/honua-release']['refs/heads/trunk'] = NEWER
    state['compare'][f'{REVIEWED}...{NEWER}'] = 'ahead'
    github.save(state)
    result, outputs = github.run_step('dr', 'Dispatch the candidate DR drill', DR_ENV)
    assert result.returncode == 0, result.stderr
    [dispatched] = github.load()['dispatches']
    assert dispatched['ref'] == 'refs/heads/trunk'
    assert dispatched['inputs'] == {'candidate_ref': SNAPSHOT}
    assert outputs['receipt_url'] == github.load()['dr_receipt_url']


@pytest.mark.parametrize('status', ['diverged', 'behind'])
def test_dr_drill_refuses_a_trunk_that_no_longer_contains_the_nightly(github, status):
    state = github.load()
    state['refs']['honua-io/honua-release']['refs/heads/trunk'] = NEWER
    state['compare'][f'{REVIEWED}...{NEWER}'] = status
    github.save(state)
    result, outputs = github.run_step('dr', 'Dispatch the candidate DR drill', DR_ENV)
    assert result.returncode != 0 and 'does not descend from reviewed' in result.stderr
    assert 'receipt_url' not in outputs


def test_dispatch_never_sends_a_commit_as_the_ref():
    calls = []
    for ref in (SERVER, 'trunk', 'heads/trunk', 'refs/pull/1/head'):
        with pytest.raises(ValueError, match='refs/heads/<branch> or refs/tags/<tag>'):
            receipts.dispatch(lambda *a: calls.append(a), 'honua-io/honua-server', 'w.yml', ref, {})
    assert calls == []


def test_dispatch_without_a_run_id_refuses():
    for response in ({}, None, {'workflow_run_id': 0}, {'workflow_run_id': True}, {'workflow_run_id': '9'}):
        with pytest.raises(LookupError, match='no run id'):
            receipts.dispatch(lambda *a: response, 'honua-io/honua-release', 'w.yml', 'refs/heads/trunk', {})


@pytest.mark.parametrize('override,problem', [
    ({'id': 2}, 'id'), ({'event': 'push'}, 'event'), ({'path': '.github/workflows/other.yml'}, 'path'),
    ({'head_sha': OTHER}, 'head'), ({'head_branch': 'feature'}, 'branch')])
def test_a_run_that_is_not_the_dispatched_producer_refuses(override, problem):
    run = {'id': 1, 'event': 'workflow_dispatch', 'path': '.github/workflows/w.yml',
           'head_sha': SHA, 'head_branch': 'trunk', **override}
    with pytest.raises(LookupError, match=problem):
        receipts.verify_run(run, run_id=1, workflow='w.yml', head_sha=SHA, head_branch='trunk')


def test_capacity_inputs_must_be_exact():
    for sha, image, lock in ((SERVER[:7], IMAGE, SNAPSHOT), (SERVER, 'ghcr.io/honua-io/honua-server:nightly', SNAPSHOT),
                             (SERVER, IMAGE, 'trunk')):
        with pytest.raises(ValueError):
            receipts.dispatch_capacity(lambda *a: pytest.fail('no API call'), 'honua-io/honua-server', sha, image, lock)


def gh_result(returncode, stdout='', stderr=''):
    return subprocess.CompletedProcess(['gh'], returncode, stdout, stderr)


def test_gh_wrapper_never_retries_a_refused_or_possibly_delivered_write():
    calls, sleeps = [], []
    answers = iter([gh_result(1, '{"message":"No ref found for: x","status":"422"}', 'gh: No ref found (HTTP 422)')])
    api = receipts.Gh(run=lambda *a, **k: calls.append(a) or next(answers), sleep=sleeps.append)
    with pytest.raises(receipts.ApiError) as refused:
        api('POST', 'repos/o/r/actions/workflows/w.yml/dispatches', {'ref': 'x'})
    assert refused.value.status == 422 and len(calls) == 1 and sleeps == []
    reset = iter([gh_result(1, '', 'read: connection reset by peer')])
    api = receipts.Gh(run=lambda *a, **k: next(reset), sleep=sleeps.append)
    with pytest.raises(receipts.ApiError):
        api('POST', 'repos/o/r/actions/workflows/w.yml/dispatches', {'ref': 'x'})
    assert sleeps == []


def test_gh_wrapper_retries_unconnected_writes_and_transient_reads():
    sleeps = []
    answers = iter([gh_result(1, '', 'error connecting to api.github.com'), gh_result(0, '{"workflow_run_id": 5}')])
    api = receipts.Gh(run=lambda *a, **k: next(answers), sleep=sleeps.append)
    assert api('POST', 'repos/o/r/actions/workflows/w.yml/dispatches', {'ref': 'x'}) == {'workflow_run_id': 5}
    answers = iter([gh_result(1, '', 'gh: API rate limit exceeded (HTTP 403)'),
                    gh_result(1, '', 'gh: Bad Gateway (HTTP 502)'), gh_result(0, '{"id": 1}')])
    api = receipts.Gh(run=lambda *a, **k: next(answers), sleep=sleeps.append)
    assert api('GET', 'repos/o/r/actions/runs/1') == {'id': 1}
    assert sleeps == [10, 10, 30]


def test_wait_for_run_polls_until_completed_or_times_out():
    states = iter([{'id': 3, 'status': 'queued'}, {'id': 3, 'status': 'in_progress'},
                   {'id': 3, 'status': 'completed', 'conclusion': 'failure'}])
    sleeps, clock = [], iter(range(0, 1000, 10))
    run = receipts.wait_for_run(lambda *a: next(states), 'o/r', 3, timeout=600, poll=60,
                                sleep=sleeps.append, clock=lambda: next(clock))
    assert run['conclusion'] == 'failure' and sleeps == [60, 60]
    with pytest.raises(TimeoutError):
        receipts.wait_for_run(lambda *a: {'id': 3, 'status': 'queued'}, 'o/r', 3, timeout=30, poll=60,
                              sleep=lambda _: None, clock=iter(range(0, 1000, 20)).__next__)


def test_capacity_receipt_url_is_the_producer_commit_for_that_run_attempt():
    commit = 'c' * 40
    message = f'capacity evidence for {SHA} (run 42/2)'
    url = receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', '2', [
        {'sha': commit, 'commit': {'message': message + '\n\nbody'}}])
    assert url == f'https://raw.githubusercontent.com/honua-io/honua-server/{commit}/capacity/{SHA}-42-2.zip'
    with pytest.raises(LookupError, match='found 0'):
        receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', '2', [])
    # Neither the pre-zip receipt title nor another attempt's commit counts.
    stale = [{'sha': commit, 'commit': {'message': f'capacity soak receipt for {SHA} (run 42)'}},
             {'sha': OTHER, 'commit': {'message': f'capacity evidence for {SHA} (run 42/1)'}}]
    with pytest.raises(LookupError, match='found 0'):
        receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', '2', stale)
    duplicate = [{'sha': commit, 'commit': {'message': message}}, {'sha': OTHER, 'commit': {'message': message}}]
    with pytest.raises(LookupError, match='found 2'):
        receipts.capacity_receipt_url('honua-io/honua-server', SHA, '42', '2', duplicate)


def test_dr_receipt_url_refuses_branch_html_and_non_https():
    raw = 'https://raw.githubusercontent.com/honua-io/honua-evidence/abc/data/receipt.json'
    assert receipts.dr_receipt_url({'receipt_url': raw}) == raw
    for url in ('https://github.com/honua-io/honua-evidence/blob/trunk/receipt.json',
                'http://raw.githubusercontent.com/honua-io/honua-evidence/abc/receipt.json',
                'refs/heads/trunk'):
        with pytest.raises(ValueError, match='immutable https'):
            receipts.dr_receipt_url({'receipt_url': url})


def test_receipt_cli_refuses_with_exit_one(tmp_path):
    location = tmp_path / 'receipt-location.json'
    location.write_text(json.dumps({'receipt_url': 'https://github.com/honua-io/honua-evidence/blob/x'}))
    refused = subprocess.run([sys.executable, str(ROOT / 'tools/nightly_receipts.py'), 'dr-url',
                              '--location', str(location)], capture_output=True, text=True)
    assert refused.returncode == 1 and refused.stderr.startswith('REFUSED:')


# ── mint publishes exactly the lock bundle ──────────────────────────────────────────────────

def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_mint_publishes_only_the_lock_bundle_as_a_lock_tag(tmp_path):
    mint_step = step(workflow()['jobs']['mint'], 'Mint and sign the next nightly lock')
    assert mint_step['env']['EXPECTED_SHA'] == '${{ github.sha }}'
    assert mint_step['env']['EXPECTED_RUN'] == '${{ github.run_id }}'
    publish = mint_step['run'].split('| tee mint.txt\n', 1)[1]
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    work = tmp_path / 'runner'
    subprocess.run(['git', 'clone', '-q', str(origin), str(work)], check=True, capture_output=True)
    (work / 'README').write_text('trunk\n')
    git(work, 'add', 'README')
    git(work, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'trunk')
    for path in ('nightly-lock/platform-lock.json', 'nightly-lock/platform-lock.sigstore.json',
                 'nightly-lock/bom.cdx.json', 'certified/gate-report.json', 'nightly-history/x/platform-lock.json',
                 'rulesets.json'):
        (work / path).parent.mkdir(parents=True, exist_ok=True)
        (work / path).write_text(path)
    (work / 'mint.txt').write_text('MINTED: 2026.1-rc.3 -> nightly-lock\n')
    runner_temp = tmp_path / 'runner-temp'
    runner_temp.mkdir()
    output = tmp_path / 'out.txt'
    subprocess.run(['bash', '-c', 'set -euo pipefail\n' + publish], cwd=work, check=True, capture_output=True,
                   env={**os.environ, 'RUNNER_TEMP': str(runner_temp), 'GITHUB_OUTPUT': str(output)})
    assert 'label=2026.1-rc.3' in output.read_text()
    files = git(origin, 'ls-tree', '-r', '--name-only', 'refs/tags/nightly-lock/2026.1-rc.3').splitlines()
    assert files == ['bom.cdx.json', 'platform-lock.json', 'platform-lock.sigstore.json']
    assert git(origin, 'log', '--format=%an <%ae>|%s', '-1', 'refs/tags/nightly-lock/2026.1-rc.3') == \
        'Mike McDougall <mike@honua.io>|chore: nightly lock 2026.1-rc.3'
    # No force: a second publication of the same label is refused by git.
    (work / 'nightly-lock/platform-lock.json').write_text('different')
    second = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + publish], cwd=work, capture_output=True,
                            env={**os.environ, 'RUNNER_TEMP': str(runner_temp), 'GITHUB_OUTPUT': str(output)})
    assert second.returncode != 0


# ── candidate-input: snapshot lineage when trunk has moved ──────────────────────────────────

@pytest.fixture
def lineage(tmp_path):
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', '--initial-branch=trunk', str(origin)], check=True)
    seed = tmp_path / 'seed'
    subprocess.run(['git', 'clone', '-q', str(origin), str(seed)], check=True, capture_output=True)
    ident = ['-c', 'user.name=t', '-c', 'user.email=t@example.invalid']

    def commit(message, **files):
        for name, text in files.items():
            (seed / name).write_text(text)
        git(seed, 'add', '-A')
        git(seed, *ident, 'commit', '-qm', message)
        return git(seed, 'rev-parse', 'HEAD')

    git(seed, 'checkout', '-q', '-b', 'trunk')
    reviewed = commit('reviewed', **{'platform-manifest.yaml': 'old\n', 'compatibility-matrix.yaml': 'old\n',
                                     'tool.py': 'v1\n'})
    git(seed, 'push', '-q', 'origin', 'trunk')

    def snapshot(parent, **files):
        git(seed, 'checkout', '-q', '--detach', parent)
        sha = commit('snapshot', **files)
        git(seed, 'push', '-q', 'origin', f'HEAD:refs/nightly-candidates/{sha}')
        git(seed, 'checkout', '-q', 'trunk')
        return sha

    good = snapshot(reviewed, **{'platform-manifest.yaml': 'candidate\n', 'compatibility-matrix.yaml': 'candidate\n'})
    sneaky = snapshot(reviewed, **{'platform-manifest.yaml': 'candidate\n', 'tool.py': 'stubbed\n'})
    git(seed, 'checkout', '-q', '-b', 'feature')
    branch_commit = commit('unreviewed', **{'tool.py': 'branch\n'})
    git(seed, 'push', '-q', 'origin', 'feature')
    off_trunk = snapshot(branch_commit, **{'platform-manifest.yaml': 'candidate\n'})
    git(seed, 'checkout', '-q', 'trunk')
    moved = commit('trunk moved', **{'tool.py': 'v2\n'})
    git(seed, 'push', '-q', 'origin', 'trunk')
    script = yaml.safe_load((ROOT / '.github/actions/candidate-input/action.yml').read_text())['runs']['steps'][0]['run']

    def run(candidate, run_sha, run_ref='refs/heads/trunk'):
        checkout = tmp_path / f'checkout-{candidate[:7]}-{run_sha[:7]}-{run_ref.rsplit("/", 1)[-1]}'
        subprocess.run(['git', 'clone', '-q', '--depth', '1', '--no-single-branch', f'file://{origin}', str(checkout)],
                       check=True, capture_output=True)
        git(checkout, 'fetch', '-q', '--depth', '1', 'origin', run_sha)
        git(checkout, 'checkout', '-q', '--detach', run_sha)
        temp = tmp_path / f'temp-{checkout.name}'
        temp.mkdir()
        result = subprocess.run(['bash', '-c', script], cwd=checkout, capture_output=True, text=True, env={
            **os.environ, 'CANDIDATE_REF': candidate, 'RUN_SHA': run_sha, 'RUN_REF': run_ref,
            'RUNNER_TEMP': str(temp)})
        return result, checkout

    return {'reviewed': reviewed, 'moved': moved, 'good': good, 'sneaky': sneaky,
            'off_trunk': off_trunk, 'branch_commit': branch_commit, 'run': run}


def test_candidate_input_inside_the_train_adopts_snapshot_data(lineage):
    result, checkout = lineage['run'](lineage['good'], lineage['reviewed'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / 'platform-manifest.yaml').read_text() == 'candidate\n'


def test_candidate_input_on_trunk_that_moved_accepts_reviewed_ancestry(lineage):
    result, checkout = lineage['run'](lineage['good'], lineage['moved'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (checkout / 'compatibility-matrix.yaml').read_text() == 'candidate\n'
    assert (checkout / 'tool.py').read_text() == 'v2\n'  # code is the dispatched trunk, data the snapshot


def test_candidate_input_off_trunk_requires_the_exact_parent(lineage):
    result, _ = lineage['run'](lineage['good'], lineage['moved'], 'refs/heads/feature')
    assert result.returncode != 0 and "is not this run's commit" in result.stdout


def test_candidate_input_refuses_a_snapshot_of_unreviewed_code(lineage):
    result, _ = lineage['run'](lineage['off_trunk'], lineage['moved'])
    assert result.returncode != 0 and 'is not reviewed trunk history' in result.stdout


def test_candidate_input_refuses_a_snapshot_that_changes_code(lineage):
    result, checkout = lineage['run'](lineage['sneaky'], lineage['moved'])
    assert result.returncode != 0 and 'unexpected candidate change: tool.py' in result.stdout
    assert (checkout / 'platform-manifest.yaml').read_text() == 'old\n'
