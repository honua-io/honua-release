#!/usr/bin/env python3
"""Trusted campaign producer. Only the unchanged complete-pack gate qualifies output.

Configuration and backend code come from the workflow's reviewed source commit.
The backend implements scenario operations; this driver never substitutes scripted
solutions, model-authored observations, local execution or a pilot denominator.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess

import agent_e2e_gate as gate
from agent_e2e_controller import Backend, BedrockModel, run_goal
from agent_e2e_backend import InProcessBackend
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, portable_path, verify_corpus
from agent_e2e_telemetry import atomic_write
from agent_e2e_state import DurableExecution, open_store
from verify_agent_e2e_campaign import REPOSITORY, WORKFLOW

ROOT = Path(__file__).resolve().parents[1]
PACK = ROOT / 'certification/agent-e2e/pack.v2.json'
CONFIG = ROOT / 'certification/agent-e2e/campaign-inputs.json'
BACKEND_CAPABILITIES = Backend.REQUIRED | {
    'scenario-setup', 'independent-oracles', 'independent-semantic-suite', 'separate-approvals',
    'fault-injection', 'bootstrap-revocation', 'durable-attempt-evidence',
    'independent-cost-observations', 'independent-expiry-cleanup', 'independent-rendering', 'native-batch'}


def require(value, reason):
    if not value:
        raise PackError(reason)


def write(path, value):
    atomic_write(Path(path), (json.dumps(value, sort_keys=True, allow_nan=False) + '\n').encode())


def source_file(relative, sha256):
    """A digest alone cannot authorize executing an arbitrary runner file."""
    portable_path(relative)
    path = gate.safe_file(ROOT, relative)
    subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--error-unmatch', '--', relative],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    require(digest_bytes(path.read_bytes()) == sha256, 'reviewed source file digest changed: ' + relative)
    return path


def controller_identity(environ=os.environ):
    require(environ.get('GITHUB_REPOSITORY') == REPOSITORY and
            environ.get('GITHUB_REF') == 'refs/heads/trunk' and
            environ.get('GITHUB_EVENT_NAME') == 'workflow_dispatch', 'producer requires trusted trunk dispatch')
    sha = environ.get('GITHUB_SHA', '')
    require(re.fullmatch('[0-9a-f]{40}', sha), 'producer source must be an exact commit')
    result = {'repository': REPOSITORY, 'workflow': WORKFLOW, 'sourceSha': sha,
              'runId': int(environ['GITHUB_RUN_ID']), 'runAttempt': int(environ['GITHUB_RUN_ATTEMPT'])}
    require(result['runId'] > 0 and result['runAttempt'] > 0, 'invalid producer run identity')
    return result


def inputs(mode='qualification'):
    require(CONFIG.is_file(), 'missing reviewed certification/agent-e2e/campaign-inputs.json; '
            'full scenario backend, immutable model/pricing, knowledge, expectations, state and bootstrap must be configured')
    subprocess.run(['git', '-C', str(ROOT), 'ls-files', '--error-unmatch', '--', str(CONFIG.relative_to(ROOT))],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = load_json(CONFIG)
    fields = {'schemaVersion', 'backend', 'model', 'state', 'expectations', 'knowledge', 'bootstrap'}
    require(fields <= set(config) <= fields | {'scopeDecision'}, 'missing or extra producer configuration')
    require(config['schemaVersion'] == 1, 'unsupported producer configuration')
    require(isinstance(config['bootstrap'], dict) and set(config['bootstrap']) == {'artifactDigest'} and
            isinstance(config['bootstrap']['artifactDigest'], str) and
            re.fullmatch(r'sha256:[0-9a-f]{64}', config['bootstrap']['artifactDigest']),
            'missing reviewed authenticated bootstrap artifact digest')
    # The current campaign includes 72 EKS cases while RC calls EKS Preview.
    # A reviewed scope decision is needed; neither truthy flags nor case removal qualify.
    if mode == 'qualification':
        decision = config.get('scopeDecision')
        require(isinstance(decision, dict) and set(decision) == {'path', 'sha256'},
                'missing reviewed EKS/Preview scope decision')
        ruling = load_json(source_file(decision['path'], decision['sha256']))
        require(set(ruling) == {'schemaVersion', 'campaignPackId', 'caseCount', 'targets',
                'eksMaturity', 'qualificationIncludesEks', 'ownerDecisionUrl'} and
                ruling['schemaVersion'] == 1 and ruling['campaignPackId'] == 'honua-agent-e2e-rc-v2' and
                ruling['caseCount'] == 252 and ruling['targets'] == load_json(PACK)['targets'] and
                ruling['eksMaturity'] == 'Preview' and ruling['qualificationIncludesEks'] is True and
                isinstance(ruling['ownerDecisionUrl'], str) and
                re.fullmatch(r'https://github.com/honua-io/honua-release/issues/[1-9][0-9]*#issuecomment-[1-9][0-9]*',
                             ruling['ownerDecisionUrl']),
                'reviewed owner ruling must explicitly reconcile 252-case EKS coverage with Preview RC scope')
    backend = config['backend']
    require(set(backend) == {'kind', 'script', 'scriptSha256', 'configuration'} and
            backend['kind'] == 'in-process', 'campaign backend must share the producer-owned durable lease')
    source_file(backend['script'], backend['scriptSha256'])
    reference = backend['configuration']
    require(isinstance(reference, dict) and set(reference) == {'path', 'sha256'}, 'missing reviewed backend configuration')
    source_file(reference['path'], reference['sha256'])
    for name in ('expectations', 'knowledge'):
        require(set(config[name]) == {'path', 'sha256'}, 'invalid reviewed ' + name)
        source_file(config[name]['path'], config[name]['sha256'])
    model = config['model']
    require(model['modelId'] == os.environ.get('TERMINAL_MODEL_NAME'), 'model differs from terminal canary baseline')
    require(model['region'] == 'us-east-1', 'campaign model region differs from reviewed baseline')
    require(re.fullmatch(r'sha256:[0-9a-f]{64}', model['pricingDigest']), 'missing pinned pricing digest')
    # Prices must themselves be reviewed source bytes, not merely a self-declared hash.
    source_file('certification/agent-e2e/model-pricing.json', model['pricingDigest'])
    pricing = load_json(ROOT/'certification/agent-e2e/model-pricing.json')
    require(all(pricing[k] == model[k] for k in ('modelId', 'returnedModelId', 'region',
                'inputUsdPerMillion', 'outputUsdPerMillion')), 'model differs from reviewed prices')
    return config


def freeze(config, lock, destination, mode, *, identity=None, now=None):
    identity = identity or controller_identity()
    now = now or datetime.now(timezone.utc)
    require(mode in ('development', 'qualification'), 'invalid campaign mode')
    pack = load_json(PACK)
    cases = gate.inventory(pack)  # Always the original 36/108/252 inventory, including EKS.
    corpus = gate.safe_file(PACK.parent, pack['corpusManifest'])
    verify_corpus(corpus.parent, load_json(corpus))
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    campaign_id = f"{identity['runId']}-{identity['runAttempt']}-{mode}"
    expected = load_json(ROOT/config['expectations']['path'])
    require(set(expected) == {'cases'} and isinstance(expected['cases'], dict) and expected['cases'] and
            (set(expected['cases']) == set(cases) if mode == 'qualification' else set(expected['cases']) <= set(cases)),
            'reviewed expectations must cover exact 252 cases for qualification or explicit development cases')
    for cid, value in expected['cases'].items():
        case = cases[cid]
        from agent_e2e_native_bindings import validate_case
        validate_case(value)
        require(set(value['assertions']) == case['assertions'],
                'reviewed expectations differ from assertions: ' + cid)
    expected.update(schemaVersion=1, campaignId=campaign_id, createdAt=now.isoformat())
    write(root/'expectations.json', expected)
    atomic_write(root/'qualification-lock.json', Path(lock).read_bytes())
    knowledge_path = ROOT/config['knowledge']['path']
    manifest = load_json(knowledge_path)
    knowledge = gate.knowledge.binding(manifest)
    graph = gate.knowledge.verify(knowledge_path.parent, manifest, knowledge)
    gate.knowledge.references(pack, graph)
    campaign = {'schemaVersion': 2, 'campaignId': campaign_id, 'campaignMode': mode,
        'packDigest': digest_bytes(PACK.read_bytes()), 'corpusDigest': digest_bytes(corpus.read_bytes()),
        'qualificationLockDigest': digest_bytes(Path(lock).read_bytes()),
        'componentBindingDigest': gate.component_binding(load_json(lock)),
        'modelId': config['model']['modelId'], 'returnedModelId': config['model']['returnedModelId'],
        'controller': identity, 'startedAt': now.isoformat(), 'expiresAt': (now+timedelta(days=7)).isoformat(),
        'totalBudgetUsd': 500, 'expectationsDigest': digest_bytes((root/'expectations.json').read_bytes()),
        'knowledge': knowledge}
    write(root/'campaign.json', campaign)
    write(root/'producer-inputs.json', config)
    return digest_bytes((root/'campaign.json').read_bytes())


def selected_cases(pack, campaign, target, shard, shards):
    require(target in pack['targets'] and type(shards) is int and 1 <= shards <= 72 and
            type(shard) is int and 0 <= shard < shards, 'invalid bounded shard')
    rows = [cid for cid, case in gate.inventory(pack).items() if case['target'] == target and
            (campaign['campaignMode'] == 'qualification' or case['variant'] in pack['developmentVariants'])]
    return rows[shard::shards]


def retain_attempt(durable, campaign, case_id, result, exported):
    """Persist only backend-produced evidence, after binding it to controller execution.

    Backend export is private controller data: a receipt and a bounded map of
    portable evidence paths to base64 bytes. No model output can invoke this API.
    The unchanged gate rechecks all predicates before attestation.
    """
    require(set(exported) == {'protocol', 'receipt', 'evidence'}, 'invalid backend attempt export')
    receipt, artifacts = exported['receipt'], exported['evidence']
    require(set(receipt) == gate.RECEIPT_FIELDS, 'incomplete backend receipt')
    for key in ('campaignId', 'packDigest', 'corpusDigest', 'qualificationLockDigest',
                'componentBindingDigest', 'modelId', 'returnedModelId', 'controller', 'knowledge', 'campaignMode'):
        require(receipt[key] == campaign[key], 'backend receipt differs from frozen ' + key)
    require(receipt['caseId'] == case_id and receipt['attempt'] == result['attempt'] and
            receipt['status'] == result['status'] and receipt['failureAttribution'] == result['failureAttribution'] and
            receipt['usage'] == result['usage'], 'backend receipt differs from actual controller execution')
    require(receipt['mode'] == 'genuine-model' and receipt['schemaVersion'] == 2 and
            receipt['receiptSchema'] == 'agent-e2e-receipt.v2', 'nonqualifying receipt format')
    case = gate.inventory(load_json(PACK))[case_id]
    require(all(receipt[k] == case[k] for k in ('scenarioId', 'variant', 'target', 'promptDigest')),
            'backend receipt differs from frozen case')
    require(set(receipt['usage']) == {'modelTurns', 'executedActions'} and
            all(type(receipt['usage'][key]) is int and 0 <= receipt['usage'][key] <= case['limits'][limit]
                for key, limit in [('modelTurns', 'maxTurns'), ('executedActions', 'maxActions')]),
            'receipt usage exceeds frozen limits')
    require(isinstance(artifacts, dict) and artifacts, 'missing independent backend artifacts')
    total = 0
    for path, encoded in artifacts.items():
        portable_path(path)
        data = base64.b64decode(encoded, validate=True)
        total += len(data)
        require(total <= 32*1024*1024, 'attempt evidence exceeds bound')
    # Preserve every attempt and original receipt, including failures. A lost
    # export/commit acknowledgement never authorizes executing that attempt again.
    identity = {k: receipt[k] for k in ('campaignId', 'caseId', 'attempt')}
    key = canonical_digest(identity)
    require(key not in durable.state.setdefault('attemptArtifacts', {}), 'attempt evidence already retained')
    raw = (json.dumps(receipt, sort_keys=True, allow_nan=False)+'\n').encode()
    registry = durable.state.setdefault('registry', {'schemaVersion': 1,
        'candidateKey': campaign['componentBindingDigest'], 'attempts': []})
    registry['attempts'].append({**{k: receipt[k] for k in ('campaignId', 'caseId', 'attempt', 'status',
        'failureAttribution', 'packDigest', 'corpusDigest', 'modelId', 'campaignMode')},
        'receiptDigest': digest_bytes(raw), 'knowledgeDigest': canonical_digest(receipt['knowledge'])})
    durable.state['attemptArtifacts'][key] = {'receipt': receipt, 'evidence': artifacts}
    durable._require_live_lease()
    durable._persist()


def next_attempt(state, campaign, case_id):
    """Resume without replay; only a retained infrastructure failure permits retry.

    run_goal separately requires backend.authorize_retry to verify the original
    independent failure evidence before it can reserve or start attempt two.
    """
    receipts = [value['receipt'] for value in state.get('attemptArtifacts', {}).values()
                if value['receipt']['campaignId'] == campaign['campaignId'] and value['receipt']['caseId'] == case_id]
    receipts.sort(key=lambda receipt: receipt['attempt'])
    require([receipt['attempt'] for receipt in receipts] in ([], [1], [1, 2]), 'invalid retained attempt sequence')
    if receipts and receipts[-1]['status'] == 'pass':
        return None
    previous = [row for row in state.get('registry', {}).get('attempts', [])
                if row['campaignMode'] == 'qualification' and row['campaignId'] != campaign['campaignId'] and
                row['packDigest'] == campaign['packDigest'] and row['corpusDigest'] == campaign['corpusDigest'] and
                row['modelId'] == campaign['modelId'] and row['knowledgeDigest'] == canonical_digest(campaign['knowledge'])]
    if campaign['campaignMode'] == 'qualification':
        require(not any(row['failureAttribution'] in ('model', 'product', 'undetermined') for row in previous),
                'prior qualification failure requires changed inputs; campaign reset forbidden')
    prior_failures = sum(row['caseId'] == case_id and row['failureAttribution'] == 'infrastructure' for row in previous)
    require(not receipts or receipts[-1]['failureAttribution'] == 'infrastructure' and
            receipts[-1]['cleanup']['status'] == 'pass', 'retained failure does not authorize infrastructure retry')
    require(prior_failures+len(receipts) < 2, 'candidate infrastructure retry budget exhausted')
    attempt = len(receipts)+1
    key = canonical_digest({'campaignId': campaign['campaignId'], 'caseId': case_id, 'attempt': attempt})
    require(key not in state.get('journals', {}), 'unresolved retained attempt requires reconciliation; do not redispatch')
    return attempt


def export_backend(backend, operation, payload, root):
    """Keep bulk private evidence off the bounded agent-observation channel.

    The backend may return small exports inline. Larger exports are written to
    one exact controller-chosen path, with an acknowledged digest. No arbitrary
    backend path, symlink or changed file is accepted. The existing Backend.call
    one-MiB stdout bound remains unchanged.
    """
    require(operation in ('export_attempt', 'export_campaign'), 'invalid evidence export operation')
    directory = Path(root)/'backend-exports'
    directory.mkdir(parents=True, exist_ok=True)
    require(not directory.is_symlink(), 'export directory cannot be a symlink')
    path = directory/(operation+'-'+canonical_digest(payload)[7:]+'.json')
    require(not path.exists() and not path.is_symlink(), 'export already exists; reconcile instead of replaying')
    result = backend.call(operation, {**payload, 'exportPath': str(path.resolve())}, timeout=600)
    if 'export' not in result:
        return result
    require(set(result) == {'protocol', 'export'} and isinstance(result['export'], dict) and
            set(result['export']) == {'path', 'sha256'} and result['export']['path'] == str(path.resolve()),
            'backend export acknowledgement names an unapproved path')
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 48*1024*1024,
            'missing or oversized private backend export')
    raw = path.read_bytes()
    require(digest_bytes(raw) == result['export']['sha256'], 'private backend export digest differs')
    value = load_json(path)
    require(path.read_bytes() == raw, 'private backend export changed during import')
    require(isinstance(value, dict) and value.get('protocol') == 'honua.agent-e2e.backend/v1',
            'unbound private backend export')
    return value


def execution_scope(pack, campaign, target, shard, shards, case_ids):
    cases = gate.inventory(pack)
    if campaign['campaignMode'] == 'qualification':
        require(not case_ids, 'qualification does not accept a reduced case inventory')
        return selected_cases(pack, campaign, target, shard, shards), BACKEND_CAPABILITIES, set(cases)
    require(isinstance(case_ids, list) and 1 <= len(case_ids) <= 12 and len(set(case_ids)) == len(case_ids),
            'development requires 1-12 explicit implemented A/B cases')
    require(all(cid in cases and cases[cid]['variant'] in pack['developmentVariants'] for cid in case_ids),
            'development cannot select held-out or unknown cases')
    targets = {cases[cid]['target'] for cid in case_ids}
    require(len(targets) == 1 and (target is None or targets == {target}), 'development cases must share one target')
    require(shard == 0, 'explicit development scope cannot be silently sharded')
    required = Backend.REQUIRED | {'scenario-setup', 'independent-oracles', 'durable-attempt-evidence',
                                  'independent-cost-observations', 'independent-expiry-cleanup'}
    assertions = set().union(*(cases[cid]['assertions'] for cid in case_ids))
    if any(assertion.startswith('approval.') for assertion in assertions):
        required.add('separate-approvals')
    if any(assertion.startswith('fault.') for assertion in assertions):
        required.add('fault-injection')
    if any(assertion.startswith('custody.') for assertion in assertions):
        required.add('bootstrap-revocation')
    if 'render.actual' in assertions:
        required.add('independent-rendering')
    if any(cid.startswith('B') for cid in case_ids):
        required.add('native-batch')
    return case_ids, required, set(case_ids)


def execute(config, bundle, target, shard, shards, *, case_ids=None, backend=None, model=None):
    root = Path(bundle)
    campaign, pack = load_json(root/'campaign.json'), load_json(PACK)
    require(campaign['controller'] == controller_identity(), 'cross-run campaign resume requires authenticated provenance; refusing rebinding')
    require(config == load_json(root/'producer-inputs.json'), 'reviewed inputs changed after freeze')
    selected, required, supported = execution_scope(pack, campaign, target, shard, shards, case_ids)
    require(selected and set(selected) <= set(load_json(root/'expectations.json')['cases']),
            'selected cases lack reviewed frozen expectations')
    target = gate.inventory(pack)[selected[0]]['target']
    backend = backend or InProcessBackend(config['backend'])
    # Qualification preflights the entire backend. Explicit development scopes
    # may integrate implemented A/B cases before all other adapters exist.
    ready = backend.call('campaign_preflight', {'campaign': campaign, 'target': target, 'caseIds': selected})
    available = set(ready.get('caseIds', []))
    coverage = available == supported if campaign['campaignMode'] == 'qualification' else supported <= available
    require(ready.get('status') == 'configured' and ready.get('readOnly') is True and
            required <= set(ready.get('capabilities', [])) and coverage and
            ready.get('qualificationLockDigest') == campaign['qualificationLockDigest'],
            'isolated scenario backend unavailable for required scope; qualification requires all 252 cases including EKS')
    store = open_store(config['state'], campaign['componentBindingDigest'])
    # State must already carry authenticated bootstrap costs. A campaign run
    # cannot mint a fresh allowance or create cloud infrastructure.
    with_execution = DurableExecution(store, root/'budget-ledger.json')
    try:
        durable = with_execution
        require('bootstrapImport' in durable.state, 'missing authenticated bootstrap accounting')
        require(durable.state['bootstrapImport']['artifactDigest'] == config['bootstrap']['artifactDigest'],
                'durable bootstrap accounting differs from reviewed import')
        require(durable.state['ledger']['schemaVersion'] == 2, 'legacy campaign requires $500/$50 operation ledger')
        ledger_attempts = {(row['campaignId'], row['caseId'], row['attempt']) for row in durable.state['ledger']['entries']}
        registered_attempts = {(row['campaignId'], row['caseId'], row['attempt'])
                               for row in durable.state.get('registry', {}).get('attempts', [])}
        require(ledger_attempts == registered_attempts,
                'candidate has unresolved accounting/receipt history; reconcile before paid execution')
        if isinstance(backend, InProcessBackend):
            backend.bind_execution(durable, campaign, root)
        campaign_key = canonical_digest(campaign)
        campaigns = durable.state.setdefault('campaigns', {})
        if campaign['campaignId'] in campaigns:
            require(campaigns[campaign['campaignId']] == campaign_key, 'retained campaign inputs changed')
        else:
            campaigns[campaign['campaignId']] = campaign_key
            durable._persist()
        durable.restore_journals(root/'journals')
        model = model or BedrockModel(config['model'])
        corpus = load_json(PACK.parent/pack['corpusManifest'])
        for case_id in selected:
            while (attempt := next_attempt(durable.state, campaign, case_id)) is not None:
                identity = {'campaignId': campaign['campaignId'], 'caseId': case_id, 'attempt': attempt}
                require(datetime.now(timezone.utc) < gate.timestamp(campaign['expiresAt']), 'campaign expired')
                result = run_goal(pack=pack, corpus=corpus, case_id=case_id, campaign=campaign, attempt=attempt,
                    backend=backend, model=model, ledger=durable.path, journal_root=root/'journals',
                    budget_update=durable.update, journal_sink=durable.event)
                write(root/'last-execution.json', result)
                exported = export_backend(backend, 'export_attempt', {**identity, 'campaign': campaign,
                    'controllerResult': result}, root)
                retain_attempt(durable, campaign, case_id, result, exported)
        write(root/'execution-summary.json', {'status': 'pass', 'campaignMode': campaign['campaignMode'],
            'casesSelected': selected, 'casesExpected': 252, 'qualification': False,
            'note': 'Execution diagnostics only; complete independent campaign validation and attestation are separate.'})
    finally:
        with_execution.close()


def materialize(durable, campaign, destination):
    root = Path(destination)
    receipts = []
    for key, value in durable.state.get('attemptArtifacts', {}).items():
        if value['receipt']['campaignId'] != campaign['campaignId']:
            continue
        path = root/'receipts'/(key[7:]+'.json')
        write(path, value['receipt'])
        receipts.append({'path': path.name, 'sha256': digest_bytes(path.read_bytes())})
        for relative, encoded in value['evidence'].items():
            portable_path(relative)
            target = root/'evidence'/relative
            data = base64.b64decode(encoded, validate=True)
            if target.exists():
                require(target.read_bytes() == data, 'conflicting retained evidence path')
            else:
                atomic_write(target, data)
    write(root/'budget-ledger.json', durable.state['ledger'])
    write(root/'attempt-registry.json', durable.state.get('registry', {}))
    return receipts


def finalize(config, bundle, *, backend=None):
    root = Path(bundle)
    campaign = load_json(root/'campaign.json')
    require(campaign['controller'] == controller_identity(), 'final producer differs from frozen controller')
    require(campaign['campaignMode'] == 'qualification', 'development shards cannot emit qualifying evidence')
    require(config == load_json(root/'producer-inputs.json'), 'reviewed inputs changed after freeze')
    backend = backend or InProcessBackend(config['backend'])
    durable = DurableExecution(open_store(config['state'], campaign['componentBindingDigest']), root/'budget-ledger.json')
    try:
        if isinstance(backend, InProcessBackend):
            backend.bind_execution(durable, campaign, root)
        require(durable.state.get('campaigns', {}).get(campaign['campaignId']) == canonical_digest(campaign),
                'final campaign differs from durably frozen execution inputs')
        receipts = materialize(durable, campaign, root)
        require(len({v['receipt']['caseId'] for v in durable.state.get('attemptArtifacts', {}).values()
                     if v['receipt']['campaignId'] == campaign['campaignId'] and v['receipt']['status'] == 'pass'}) == 252,
                'qualification incomplete: 252 actual goal-and-cleanup cases including EKS required')
        # Independent semantic/operation evidence must already have been paid
        # and retained by the backend. Finalization only exports, never probes.
        exported = export_backend(backend, 'export_campaign', {'campaign': campaign}, root)
        require(set(exported) == {'protocol', 'evidence', 'knowledgeManifest', 'semanticReport', 'operationReports'},
                'incomplete independent campaign evidence')
        for relative, encoded in exported['evidence'].items():
            portable_path(relative)
            data = base64.b64decode(encoded, validate=True)
            require(len(data) <= 32*1024*1024, 'campaign evidence file exceeds bound')
            target = root/'evidence'/relative
            require(not target.exists() or target.read_bytes() == data, 'campaign evidence overwrites attempt evidence')
            atomic_write(target, data)
        trusted = {'schemaVersion': 2, 'campaignId': campaign['campaignId'],
            'campaignDigest': digest_bytes((root/'campaign.json').read_bytes()), 'controller': campaign['controller'],
            'receipts': receipts, 'budgetLedgerDigest': digest_bytes((root/'budget-ledger.json').read_bytes()),
            'attemptRegistryDigest': digest_bytes((root/'attempt-registry.json').read_bytes()),
            **{k: exported[k] for k in ('knowledgeManifest', 'semanticReport', 'operationReports')}}
        write(root/'trusted-inventory.json', trusted)
        result = gate.validate(PACK, root/'campaign.json', root/'qualification-lock.json', root/'trusted-inventory.json',
            root/'receipts', root/'evidence', root/'budget-ledger.json', root/'expectations.json', root/'attempt-registry.json')
        write(root/'qualification-summary.json', result)
        return result
    finally:
        durable.close()


def reconcile_accounting(config, bundle):
    """Recover retained charges in the same exact trusted campaign invocation."""
    root = Path(bundle)
    campaign = load_json(root/'campaign.json')
    require(campaign['controller'] == controller_identity(), 'accounting recovery differs from frozen controller provenance')
    require(config == load_json(root/'producer-inputs.json'), 'accounting recovery inputs changed after freeze')
    durable = DurableExecution(open_store(config['state'], campaign['componentBindingDigest']), root/'budget-ledger.json')
    try:
        require(durable.state.get('campaigns', {}).get(campaign['campaignId']) == canonical_digest(campaign),
                'accounting recovery differs from retained campaign')
        from agent_e2e_native_costs import reconcile_measured
        result = reconcile_measured(durable, campaign)
        write(root/'accounting-reconciliation.json', result)
        return result
    finally:
        durable.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['freeze', 'execute', 'finalize', 'reconcile-accounting'])
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--lock', type=Path)
    parser.add_argument('--candidate-provenance', type=Path)
    parser.add_argument('--mode', choices=['development', 'qualification'], default='development')
    parser.add_argument('--target', choices=list(__import__('agent_e2e_pack').TARGETS))
    parser.add_argument('--shard', type=int, default=0)
    parser.add_argument('--shards', type=int, default=6)
    parser.add_argument('--case-id', action='append', dest='case_ids')
    args = parser.parse_args(argv)
    try:
        mode = args.mode if args.operation == 'freeze' else load_json(args.bundle/'campaign.json')['campaignMode']
        config = inputs(mode)
        if args.operation == 'freeze':
            require(args.lock is not None, 'candidate lock is required')
            require(args.candidate_provenance is not None, 'authenticated exact candidate run/attempt provenance is required')
            provenance = load_json(args.candidate_provenance)
            require(provenance.get('schemaVersion') == 1 and
                    provenance.get('qualificationLockDigest') == digest_bytes(args.lock.read_bytes()),
                    'candidate authentication differs from frozen lock bytes')
            frozen = freeze(config, args.lock, args.bundle, args.mode)
            atomic_write(args.bundle/'candidate-provenance.json', args.candidate_provenance.read_bytes())
            print(json.dumps({'status': 'frozen', 'campaignDigest': frozen, 'qualification': False}))
        elif args.operation == 'execute':
            execute(config, args.bundle, args.target, args.shard, args.shards, case_ids=args.case_ids)
        elif args.operation == 'reconcile-accounting':
            print(json.dumps(reconcile_accounting(config, args.bundle)))
        else:
            print(json.dumps(finalize(config, args.bundle)))
        return 0
    except Exception as exc:
        # Provider/backend failures can include secrets; only controlled errors
        # or the exception class enter public Actions logs.
        reason = str(exc) if isinstance(exc, PackError) else type(exc).__name__
        print(json.dumps({'status': 'blocked', 'qualification': False, 'reason': reason}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
