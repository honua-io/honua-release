"""Fenced private browser subprocess bound to original native save and custody."""
from __future__ import annotations
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_budget import validate as validate_budget, money
from agent_e2e_native_authoring_bindings import replay
from agent_e2e_pack import canonical_digest
from agent_e2e_studio_render import project
from controller_archive import require
from browser_session import STORAGE_ENV, binding, observe, storage
from worker_mcp_configuration import verify_injected_secret

ROOT = Path(__file__).resolve().parents[2]
COMMAND = ['/usr/local/bin/node', 'tools/agent_e2e_studio_render_cli.mjs']
RUNTIME_FILE = Path('/opt/honua-render/runtime.json')
IDENTITY = ('campaignId', 'caseId', 'attempt', 'campaignMode', 'componentBindingDigest')


def authorize(durable, context, coverage_digest):
    attempt = durable._fixture_identity(context)
    validate_budget(durable.state['ledger'])
    allocation = durable.state.get('nativeOverheadAllocations', {}).get(attempt)
    require(isinstance(allocation, dict) and allocation.get('candidateKey') == durable.store.candidate and
        allocation.get('identity') == {key: context[key] for key in ('campaignId', 'caseId', 'attempt')} and
        allocation.get('coverageDigest') == coverage_digest and allocation.get('overheadStatus') == 'liability-retained',
        'browser CPU requires original admitted controller overhead coverage')
    covered = [row for row in allocation['bindings'] if 'controller-runtime' in row['categories']]
    require(len(covered) == 1 and money(covered[0]['perAttemptCeilingUsd']) > 0,
        'browser CPU has no positive bounded original controller allowance')
    for bound in allocation['bindings']:
        rows = [row for row in durable.state['ledger'].get('operations', []) if row['id'] == bound['reservationId']]
        require(len(rows) == 1 and rows[0]['state'] == 'reserved' and rows[0]['actualUsd'] is None and
            all(rows[0][key] == bound[key] for key in ('basisDigest', 'operationId', 'purpose')) and
            money(bound['perAttemptCeilingUsd']) > 0 and money(bound['perAttemptCeilingUsd'])*bound['maximumAttempts'] <= money(rows[0]['amountUsd']),
            'browser overhead original operation is missing, settled or rebound')
        prior = [binding for other in durable.state['nativeOverheadAllocations'].values()
                 for binding in other['bindings'] if binding['reservationId'] == bound['reservationId']]
        require(len(prior) <= bound['maximumAttempts'] and
            all(row['basisDigest'] == bound['basisDigest'] for row in prior) and
            sum((money(row['perAttemptCeilingUsd']) for row in prior), Decimal(0)) <= money(rows[0]['amountUsd']),
            'browser controller shared capacity exceeded original allowance')
    return attempt


def saved_original(durable, context, native_binding, selection):
    attempt = canonical_digest({key: context[key] for key in ('campaignId', 'caseId', 'attempt')})
    require(isinstance(native_binding, dict) and set(native_binding) == {'name', 'declaration', 'report'},
        'browser requires the original finite native save binding')
    report = native_binding['report']
    require(durable.state.get('nativeBindings', {}).get(attempt+'/'+native_binding['name']+'/saved') == report and
        report.get('name') == native_binding['name'] and
        report.get('selection') == {key: selection[key] for key in ('ordinal', 'actionId', 'request', 'publicResult')},
        'browser cannot select another saved version or caller-declared native report')
    records = durable.state.get('scenarioOperations', {})
    for pair in report['records'].values():
        reference = pair['response']['intentReference']
        require(records.get(reference['key']) == pair['intent'] and canonical_digest(pair['intent']) == reference['sha256'] and
            any(value == pair['response'] for value in records.values()), 'browser native HTTP originals are absent or changed')
    journal = durable.state.get('journals', {}).get(attempt, [])
    identities = replay(report, native_binding['declaration'], {**context,
        'modelActions': [row for row in journal if row.get('event') == 'action']})
    return identities


def run_browser(*, contract, runtime, state, authorize_effect, timeout, command=COMMAND, environment=None, work_directory='/work'):
    """No shell, credential env, stderr retention, traces or temporary cookies."""
    if environment is not None:
        require(isinstance(environment, dict) and set(environment) <= {
            'PATH', 'LD_LIBRARY_PATH', 'HONUA_RENDER_PLAYWRIGHT', 'HONUA_RENDER_CHROMIUM', 'PLAYWRIGHT_BROWSERS_PATH'} and
            all(isinstance(value, str) for value in environment.values()), 'browser dependency override contains unreviewed environment authority')
    private_directory = tempfile.TemporaryDirectory(prefix='private-browser-', dir=work_directory)
    child_env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': private_directory.name, 'TMPDIR': private_directory.name,
        'PLAYWRIGHT_BROWSERS_PATH': '/opt/honua-render/browsers',
        'HONUA_RENDER_PLAYWRIGHT': '/opt/honua-render/node_modules/playwright-core/index.mjs'}
    if environment is not None:
        # Tests may select exact installed dependency paths. Preserve private
        # HOME/TMPDIR and the fixed browser path; never import ambient secrets.
        child_env.update(environment)
    contract_json = json.dumps(contract, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    payload = json.dumps({'contractJson': contract_json, 'runtime': runtime, 'storageState': state},
                        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()+b'\n'
    try:
        require(len(payload) <= 2*1024*1024, 'browser private input exceeds fixed CLI bound')
        authorize_effect()
        process = subprocess.Popen(command, cwd=ROOT, env=child_env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, bufsize=0)
    except Exception:
        private_directory.cleanup()
        raise ValueError('private browser runtime could not start') from None
    deadline = time.monotonic()+timeout
    selector, buffer, sequence, report, complete = selectors.DefaultSelector(), b'', 0, None, False
    try:
        selector.register(process.stdout, selectors.EVENT_READ)
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE)
        pending = payload
        while True:
            remaining = deadline-time.monotonic()
            require(remaining > 0, 'private browser observation deadline exhausted')
            ready = selector.select(min(remaining, 1))
            if not ready:
                require(process.poll() is None, 'private browser exited without complete protocol')
                continue
            if any(key.fileobj is process.stdin for key, _ in ready):
                pending = pending[os.write(process.stdin.fileno(), pending[:65536]):]
                if not pending:
                    selector.unregister(process.stdin)
            if not any(key.fileobj is process.stdout for key, _ in ready):
                continue
            block = os.read(process.stdout.fileno(), 65536)
            if not block:
                break
            buffer += block
            require(len(buffer) <= 8*1024*1024+65536, 'private browser event exceeds fixed byte bound')
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                require(len(line) <= 8*1024*1024, 'private browser event exceeds fixed byte bound')
                event = json.loads(line)
                require(isinstance(event, dict) and not complete, 'private browser protocol order differs')
                kind = event.get('type')
                if kind == 'authorize':
                    require(set(event) == {'type', 'id'} and type(event['id']) is int and event['id'] == sequence+1,
                        'private browser fence sequence differs')
                    authorize_effect(); sequence += 1
                    require(not pending, 'private browser authorized before consuming original input')
                    pending = json.dumps({'type': 'authorized', 'id': sequence}).encode()+b'\n'
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                elif kind == 'report':
                    require(set(event) == {'type', 'report'} and report is None and sequence > 0 and
                        event['report'].get('browserRuntime') == runtime, 'private browser runtime/report identity differs')
                    report = event['report']
                elif kind == 'complete':
                    require(set(event) == {'type'} and report is not None, 'private browser completion lacks original report')
                    complete = True
                else:
                    raise ValueError('private browser observation failed')
        require(not buffer and complete and process.wait(timeout=max(.01, deadline-time.monotonic())) == 0,
            'private browser did not complete successfully')
        authorize_effect()
        return report
    except Exception:
        raise ValueError('private browser observation failed; preserve original attempt and liabilities') from None
    finally:
        selector.close()
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        process.stdin.close(); process.stdout.close()
        private_directory.cleanup()


def observe_original_render(*, contract, selection, native_binding, durable, context, configuration):
    require(isinstance(configuration, dict) and set(configuration) == {'schemaVersion', 'storageState', 'sessionCustody', 'overheadCoverageDigest', 'runtime', 'timeoutSeconds'} and
        configuration['schemaVersion'] == 1 and configuration['timeoutSeconds'] == 120,
        'browser observation needs exact reviewed custody, runtime and bounded overhead')
    binding(configuration['storageState'])
    reference = configuration['sessionCustody']
    require(isinstance(reference, dict) and set(reference) == {'key', 'sha256'}, 'original human Console capture reference required')
    custody = durable.state.get('scenarioOperations', {}).get(reference['key'])
    require(isinstance(custody, dict) and canonical_digest(custody) == reference['sha256'] and
        custody.get('schema') == 'honua.private-console-human-session/v1' and custody.get('producer') == 'private-console-human-capture' and
        custody.get('candidateKey') == contract.get('candidateKey') == context['componentBindingDigest'] == durable.store.candidate and
        custody.get('storageState') == configuration['storageState'] and custody.get('qualification') is False,
        'browser session cannot come from caller-declared human metadata or another storage version')
    def fenced():
        return authorize(durable, context, configuration['overheadCoverageDigest'])
    attempt = fenced()
    saved = saved_original(durable, context, native_binding, selection)
    require(contract['scope']['versionId'] == saved['savedVersionId'] and
        custody['configuration']['consoleProfile']['serverUri'].rstrip('/') ==
            native_binding['declaration']['observers'][str(context['attempt'])]['endpoint'],
        'browser selected a different original saved version or native observer target')
    actual_runtime = json.loads(RUNTIME_FILE.read_text())
    require(actual_runtime == configuration['runtime'] and actual_runtime['playwright'] == '1.61.1', 'browser image runtime differs from reviewed canonical package/browser')
    verify_injected_secret(configuration['storageState'], STORAGE_ENV, 'us-east-1')
    state = storage(os.environ.get(STORAGE_ENV))
    fenced()
    observed = observe(custody['configuration'], state)
    require(observed['session'] == custody['observed']['session'] == contract['session'] | {'authenticated': True} and
        observed['nativeTarget'] == custody['observed']['nativeTarget'] and observed['deployment'] == custody['observed']['deployment'],
        'actual human Console session, native principal or production deployment changed since original capture')
    records = durable.state.setdefault('scenarioOperations', {})
    key = attempt+'/browser-render/'+canonical_digest(contract)[7:]
    require(key+'/intent' not in records and key+'/report' not in records, 'browser observation already started; retain original result without replay')
    records[key+'/intent'] = {'producer': 'private-browser-render', 'candidateKey': durable.store.candidate,
        'context': {name: context[name] for name in IDENTITY}, 'contractDigest': canonical_digest(contract),
        'sessionCustody': reference, 'nativeBindingDigest': canonical_digest(native_binding['report'])}
    durable._persist()
    report = run_browser(contract=contract, runtime=actual_runtime, state=state, authorize_effect=fenced, timeout=120)
    project(report, contract)
    sensitive = [row['value'] for row in state['cookies']]+[row['value'] for origin in state['origins'] for row in origin.get('localStorage', [])]
    serialized = json.dumps(report, ensure_ascii=False)
    require(all(value not in serialized for value in sensitive if len(value) >= 16), 'browser report contains private credential material')
    fenced()
    record = {'producer': 'private-browser-render', 'candidateKey': durable.store.candidate,
        'context': {name: context[name] for name in IDENTITY}, 'contractDigest': canonical_digest(contract),
        'report': copy.deepcopy(report), 'sessionCustody': reference, 'sessionObservation': observed,
        'storageState': configuration['storageState'], 'nativeBindingDigest': canonical_digest(native_binding['report']),
        'overheadCoverageDigest': configuration['overheadCoverageDigest'], 'producerTaskArn': os.environ['PILOT_CONTROLLER_TASK_ARN']}
    require(key+'/report' not in records, 'browser original report cannot be overwritten')
    records[key+'/report'] = record
    durable._persist()
    return {'report': report, 'reference': {'key': key+'/report', 'sha256': canonical_digest(record)}}
