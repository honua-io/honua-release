#!/usr/bin/env python3
"""Validate a complete, controller-attested agent E2E campaign.

SECURITY BOUNDARY: --campaign and --trusted-inventory are external trusted
inputs, never agent output. The caller MUST verify the inventory artifact's
GitHub attestation against the expected repository, workflow, source and run
before invoking this command. This offline validator does not authenticate a
JSON file merely because it is named 'trusted'. Receipt hashes bind all oracle
and evidence hashes transitively. The attesting controller must keep the
oracle and signing credentials outside the tested agent's boundary.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import importlib.util
import json
from pathlib import Path
import re
from typing import Any

import agent_e2e_budget
import agent_e2e_knowledge as knowledge

_ORACLE_PATH = Path(__file__).resolve().parents[1] / 'certification/agent-e2e/corpus/oracle.py'
_ORACLE_SPEC = importlib.util.spec_from_file_location('agent_e2e_independent_oracle', _ORACLE_PATH)
oracle_predicates = importlib.util.module_from_spec(_ORACLE_SPEC)
_ORACLE_SPEC.loader.exec_module(oracle_predicates)
FROZEN_PACK_BYTES = (_ORACLE_PATH.parents[1] / 'pack.v2.json').read_bytes()


class Invalid(ValueError):
    """An incomplete or untrusted qualification."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Invalid(message)


def digest(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def canonical_digest(value: Any) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=False, allow_nan=False).encode('utf-8'))


def component_binding(lock: dict) -> str:
    """Stable runtime/content identity across permitted post-gate metadata.

    Gate fixture declarations are aggregated by the minter after qualification;
    that existing validated transformation does not change shipped components.
    Corpus bytes and the original qualification lock are bound separately.
    """
    require(all(k in lock for k in ('components', 'contentDigests')),
            'lock missing immutable component binding')
    return canonical_digest({k: lock[k] for k in ('components', 'contentDigests')})


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, f'duplicate JSON key: {key}')
        result[key] = value
    return result


def read_json(path: Path) -> dict:
    def invalid_constant(value: str) -> None:
        raise Invalid(f'non-finite JSON number: {value}')
    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_pairs,
                       parse_constant=invalid_constant)
    require(isinstance(value, dict), f'expected JSON object: {path.name}')
    return value


def exact_keys(value: Any, keys: set[str], name: str) -> None:
    require(isinstance(value, dict) and set(value) == keys, f'{name}: unexpected/missing fields')


def timestamp(value: Any) -> datetime:
    require(isinstance(value, str), 'timestamp must be a string')
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise Invalid('invalid timestamp') from exc
    require(result.tzinfo is not None and result.utcoffset() is not None,
            'timestamp must include timezone')
    return result.astimezone(timezone.utc)


def amount(value: Any) -> Decimal:
    require(type(value) in (int, float), 'cost must be a finite number, not a boolean')
    result = Decimal(str(value))
    require(result.is_finite() and result >= 0, 'cost must be finite and nonnegative')
    return result


def safe_file(root: Path, relative: Any) -> Path:
    require(isinstance(relative, str) and relative and '\\' not in relative,
            'evidence path must be a nonempty portable relative path')
    require(not Path(relative).is_absolute() and ':' not in relative
            and all(part not in ('', '.', '..') for part in relative.split('/')),
            'unsafe evidence path')
    candidate = root / relative
    require(candidate.is_file(), f'missing artifact: {relative}')
    require(candidate.resolve().is_relative_to(root.resolve()), 'artifact escapes evidence root')
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        require(not parent.is_symlink(), 'symlink evidence is not allowed')
    return candidate


def evidence(root: Path, ref: Any) -> Path:
    exact_keys(ref, {'path', 'sha256'}, 'evidence reference')
    require(isinstance(ref['sha256'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', ref['sha256']),
            'invalid evidence digest')
    path = safe_file(root, ref['path'])
    require(digest(path.read_bytes()) == ref['sha256'], f'artifact hash mismatch: {ref["path"]}')
    return path


def inventory(pack: dict) -> dict[str, dict]:
    require(type(pack.get('schemaVersion')) is int and pack['schemaVersion'] == 1
            and pack.get('packId') == 'honua-agent-e2e-rc-v2',
            'unsupported pack')
    require(pack.get('variants') == ['A', 'B', 'C'], 'pack must contain frozen A/B/C variants')
    targets = ['local-docker', 'aws-ecs-redis-on', 'aws-eks', 'aws-lambda', 'aws-batch']
    require(pack.get('targets') == targets, 'pack target inventory changed')
    expected_scenarios = {f'P{i:02}': targets[:3] for i in range(1, 25)}
    expected_scenarios.update({f'L{i:02}': ['aws-lambda'] for i in range(1, 7)})
    expected_scenarios.update({f'B{i:02}': ['aws-batch'] for i in range(1, 7)})
    seen = set()
    cases = {}
    for scenario in pack.get('scenarios', []):
        sid = scenario['id']
        require(sid in expected_scenarios and sid not in seen, 'unknown/duplicate scenario')
        seen.add(sid)
        require(scenario['targets'] == expected_scenarios[sid], f'{sid}: wrong targets')
        assertions = scenario['assertions']
        require(isinstance(assertions, list) and assertions and
                all(isinstance(a, str) and a for a in assertions) and
                len(set(assertions)) == len(assertions), f'{sid}: invalid assertions')
        require(set(scenario['prompts']) == {'A', 'B', 'C'}, f'{sid}: missing prompt variant')
        exact_keys(scenario['limits'], {'maxTurns', 'maxActions', 'timeoutSeconds'}, f'{sid}: limits')
        require(all(type(v) is int and v > 0 for v in scenario['limits'].values()),
                f'{sid}: limits must be positive integers')
        for variant in pack['variants']:
            prompt = scenario['prompts'][variant]
            require(isinstance(prompt, str) and prompt.strip(), f'{sid}: empty prompt')
            for target in scenario['targets']:
                key = f'{sid}:{variant}:{target}'
                cases[key] = {'scenarioId': sid, 'variant': variant, 'target': target,
                              'promptDigest': digest(prompt.encode('utf-8')),
                              'assertions': set(assertions), 'limits': scenario['limits']}
    require(seen == set(expected_scenarios) and len(cases) == 252, 'pack is not the frozen 252-case inventory')
    return cases


RECEIPT_FIELDS = set(('schemaVersion receiptSchema campaignId caseId scenarioId variant target attempt mode '
                      'packDigest corpusDigest promptDigest qualificationLockDigest componentBindingDigest '
                      'modelId returnedModelId controller startedAt completedAt status failureAttribution '
                      'namespace costs usage cleanup oracle transcript failureEvidence '
                      'knowledge workerKnowledge runtimeInterfaces campaignMode').split())


def validate(pack_path: Path, campaign_path: Path, lock_path: Path, trusted_path: Path,
             receipts_root: Path, evidence_root: Path, budget_path: Path, expectations_path: Path,
             registry_path: Path,
             *, now: datetime | None = None) -> dict:
    pack, campaign, lock, trusted = map(read_json, (pack_path, campaign_path, lock_path, trusted_path))
    cases = inventory(pack)
    require(pack_path.read_bytes() == FROZEN_PACK_BYTES, 'pack differs from reviewed source inventory')
    exact_keys(campaign, set(('schemaVersion campaignId packDigest corpusDigest qualificationLockDigest '
                             'componentBindingDigest modelId returnedModelId controller startedAt expiresAt '
                             'totalBudgetUsd expectationsDigest knowledge campaignMode').split()), 'expected campaign')
    require(type(campaign['schemaVersion']) is int and campaign['schemaVersion'] == 2,
            'unsupported campaign')
    require(campaign['campaignMode'] == 'qualification', 'development cannot qualify')
    knowledge.validate_binding(campaign['knowledge'])
    for key in ('campaignId', 'modelId', 'returnedModelId'):
        require(isinstance(campaign[key], str) and campaign[key].strip(), f'empty {key}')
    exact_keys(campaign['controller'], {'repository', 'workflow', 'sourceSha', 'runId', 'runAttempt'},
               'controller identity')
    controller = campaign['controller']
    require(controller['repository'] == 'honua-io/honua-release', 'wrong controller repository')
    require(isinstance(controller['workflow'], str) and controller['workflow'].startswith('.github/workflows/'),
            'invalid controller workflow')
    require(isinstance(controller['sourceSha'], str) and re.fullmatch(r'[0-9a-f]{40}', controller['sourceSha']),
            'controller requires exact source revision')
    require(all(type(controller[k]) is int and controller[k] > 0 for k in ('runId', 'runAttempt')),
            'invalid controller run identity')
    require(digest(pack_path.read_bytes()) == campaign['packDigest'], 'wrong frozen pack')
    corpus = safe_file(pack_path.parent, pack['corpusManifest'])
    require(digest(corpus.read_bytes()) == campaign['corpusDigest'], 'wrong corpus manifest')
    corpus_manifest = read_json(corpus)
    require(isinstance(corpus_manifest.get('files'), list) and corpus_manifest['files'], 'missing corpus file inventory')
    corpus_paths = set()
    for row in corpus_manifest['files']:
        require(row['path'] not in corpus_paths, 'duplicate corpus artifact')
        corpus_paths.add(row['path'])
        artifact = safe_file(corpus.parent, row['path'])
        require(type(row['bytes']) is int and row['bytes'] == artifact.stat().st_size and
                digest(artifact.read_bytes()) == 'sha256:' + row['sha256'], 'corpus artifact bytes changed')
    require(digest(lock_path.read_bytes()) == campaign['qualificationLockDigest'], 'wrong qualification lock')
    require(component_binding(lock) == campaign['componentBindingDigest'], 'wrong component binding')
    start, expiry = timestamp(campaign['startedAt']), timestamp(campaign['expiresAt'])
    require(0 < (expiry - start).total_seconds() <= 7 * 24 * 60 * 60,
            'campaign validity must be bounded to at most seven days')
    require(digest(expectations_path.read_bytes()) == campaign['expectationsDigest'], 'wrong frozen expectations')
    expectations = read_json(expectations_path)
    exact_keys(expectations, {'schemaVersion', 'campaignId', 'createdAt', 'cases'}, 'frozen expectations')
    require(type(expectations['schemaVersion']) is int and expectations['schemaVersion'] == 1
            and expectations['campaignId'] == campaign['campaignId'], 'wrong expectation campaign')
    require(timestamp(expectations['createdAt']) <= start, 'expectations were not frozen before execution')
    require(isinstance(expectations['cases'], dict) and set(expectations['cases']) == set(cases),
            'expectations must cover exact frozen inventory')
    for cid, value in expectations['cases'].items():
        exact_keys(value, {'assertions', 'cleanup'}, f'{cid}: frozen expectations')
        require(isinstance(value['assertions'], dict) and set(value['assertions']) == cases[cid]['assertions'],
                f'{cid}: expectation assertions differ from pack')
    current = now or datetime.now(timezone.utc)
    require(start <= current <= expiry, 'campaign is stale or has not started')
    require(amount(campaign['totalBudgetUsd']) == 500 and amount(pack['totalBudgetUsd']) == 500,
            'campaign budget must remain $500')
    exact_keys(trusted, {'schemaVersion', 'campaignId', 'campaignDigest', 'controller', 'receipts', 'budgetLedgerDigest',
                         'attemptRegistryDigest', 'knowledgeManifest', 'semanticReport'},
               'trusted inventory')
    require(type(trusted['schemaVersion']) is int and trusted['schemaVersion'] == 1,
            'unsupported trusted inventory')
    require(trusted['campaignDigest'] == digest(campaign_path.read_bytes()) and
            trusted['campaignId'] == campaign['campaignId'] and trusted['controller'] == controller,
            'trusted inventory does not bind expected campaign/controller')
    manifest_path = evidence(evidence_root, trusted['knowledgeManifest'])
    graph = knowledge.verify(manifest_path.parent, read_json(manifest_path), campaign['knowledge'])
    knowledge.references(pack, graph)
    # The deterministic suite is a separately observed requirement, never part
    # of the 252 model-success denominator.
    from agent_e2e_semantics import validate_report
    semantic_report = read_json(evidence(evidence_root, trusted['semanticReport']))
    validate_report(semantic_report, campaign, pack['targets'], evidence_root, evidence)
    require(isinstance(trusted['receipts'], list) and trusted['receipts'], 'missing trusted receipt inventory')
    require(digest(budget_path.read_bytes()) == trusted['budgetLedgerDigest'], 'wrong candidate budget ledger')
    ledger = read_json(budget_path)
    require(ledger['candidateKey'] == campaign['componentBindingDigest'], 'budget belongs to another candidate')
    budget_summary = agent_e2e_budget.validate(ledger)
    require(digest(registry_path.read_bytes()) == trusted['attemptRegistryDigest'], 'wrong candidate attempt registry')
    registry = read_json(registry_path)
    exact_keys(registry, {'schemaVersion', 'candidateKey', 'attempts'}, 'candidate attempt registry')
    require(type(registry['schemaVersion']) is int and registry['schemaVersion'] == 1
            and registry['candidateKey'] == campaign['componentBindingDigest']
            and isinstance(registry['attempts'], list), 'invalid candidate attempt registry')
    registered = {}
    prior_infrastructure_failures: dict[str, int] = defaultdict(int)
    for row in registry['attempts']:
        exact_keys(row, {'campaignId', 'caseId', 'attempt', 'status', 'failureAttribution', 'packDigest',
                         'corpusDigest', 'modelId', 'receiptDigest', 'campaignMode', 'knowledgeDigest'}, 'registered attempt')
        require(row['campaignMode'] in ('development', 'qualification'), 'invalid registered campaign mode')
        require(isinstance(row['knowledgeDigest'], str) and re.fullmatch(r'sha256:[0-9a-f]{64}', row['knowledgeDigest']),
                'invalid registered knowledge binding')
        key = (row['campaignId'], row['caseId'], row['attempt'])
        require(type(row['attempt']) is int and row['attempt'] in (1, 2) and key not in registered,
                'duplicate/invalid registered attempt')
        require(row['status'] in ('pass', 'fail', 'blocked') and
                ((row['status'] == 'pass' and row['failureAttribution'] is None) or
                 (row['status'] != 'pass' and row['failureAttribution'] in ('model', 'product', 'infrastructure', 'undetermined'))),
                'invalid registered outcome')
        registered[key] = row
        if (row['campaignMode'] == 'qualification' and row['knowledgeDigest'] == canonical_digest(campaign['knowledge'])
                and row['campaignId'] != campaign['campaignId'] and all(row[k] == campaign[k]
                for k in ('packDigest', 'corpusDigest', 'modelId'))):
            require(row['failureAttribution'] not in ('model', 'product', 'undetermined'),
                    'prior model/product failure requires changed qualification inputs; campaign reset forbidden')
            if row['failureAttribution'] == 'infrastructure':
                prior_infrastructure_failures[row['caseId']] += 1
    ledger_attempts = {(r['campaignId'], r['caseId'], r['attempt']) for r in ledger['entries']}
    require(set(registered) == ledger_attempts, 'attempt registry omits candidate budget history')
    settled: dict[tuple, Decimal] = defaultdict(Decimal)
    for row in ledger['entries']:
        registered_row = registered[(row['campaignId'], row['caseId'], row['attempt'])]
        require(row.get('campaignMode', 'qualification') == registered_row['campaignMode'], 'ledger/registry mode mismatch')
        if row['campaignId'] == campaign['campaignId']:
            require(row['state'] == 'settled', 'current campaign has unsettled resource liability')
            require(type(row.get('attempt')) is int and row['attempt'] in (1, 2), 'ledger attempt missing/invalid')
            require(row['caseId'] in cases, 'current campaign ledger contains unknown case')
            settled[(row['caseId'], row['attempt'], row['kind'])] += agent_e2e_budget.money(row['actualUsd'])
    groups: dict[str, list] = defaultdict(list)
    names, paths = set(), set()
    total = Decimal(0)
    for ref in trusted['receipts']:
        path = evidence(receipts_root, ref)
        require(ref['path'] not in paths, 'duplicate receipt inventory entry')
        paths.add(ref['path'])
        receipt = read_json(path)
        exact_keys(receipt, RECEIPT_FIELDS, 'receipt')
        require(type(receipt['schemaVersion']) is int and receipt['schemaVersion'] == 2 and
                receipt['receiptSchema'] == 'agent-e2e-receipt.v2', 'unsupported receipt schema')
        require(receipt['caseId'] in cases, 'unknown case')
        require(receipt['campaignMode'] == 'qualification', 'development receipt cannot qualify')
        require(receipt['knowledge'] == campaign['knowledge'], 'wrong receipt knowledge binding')
        worker_manifest = read_json(evidence(evidence_root, receipt['workerKnowledge']))
        require(knowledge.binding(worker_manifest) == campaign['knowledge'], 'wrong worker knowledge manifest')
        interfaces = read_json(evidence(evidence_root, receipt['runtimeInterfaces']))
        exact_keys(interfaces, {'caseId', 'attempt', 'interfaces'}, 'runtime interfaces')
        require(interfaces['caseId'] == receipt['caseId'] and interfaces['attempt'] == receipt['attempt'],
                'runtime interfaces belong to another attempt')
        require(isinstance(interfaces['interfaces'], list) and bool(interfaces['interfaces']),
                'no observed runtime interfaces')
        for interface in interfaces['interfaces']:
            exact_keys(interface, {'kind', 'operation', 'schema'}, 'runtime interface')
            require(interface['kind'] in ('mcp', 'http', 'cli', 'spec') and
                    isinstance(interface['operation'], str) and bool(interface['operation']), 'invalid interface')
            evidence(evidence_root, interface['schema'])
        require(receipt['mode'] == 'genuine-model', 'mock/test receipts cannot qualify')
        cid = receipt['caseId']
        require(cid in cases, f'unknown case: {cid}')
        expected = cases[cid]
        for field in ('scenarioId', 'variant', 'target', 'promptDigest'):
            require(receipt[field] == expected[field], f'{cid}: wrong {field}')
        for field in ('campaignId', 'packDigest', 'corpusDigest', 'qualificationLockDigest',
                      'componentBindingDigest', 'modelId', 'returnedModelId', 'controller'):
            require(canonical_digest(receipt[field]) == canonical_digest(campaign[field]), f'{cid}: wrong {field}')
        require(type(receipt['attempt']) is int and receipt['attempt'] in (1, 2), 'invalid attempt')
        registered_attempt = registered.get((campaign['campaignId'], cid, receipt['attempt']))
        require(registered_attempt is not None and registered_attempt['receiptDigest'] == ref['sha256'] and
                registered_attempt['campaignMode'] == receipt['campaignMode'] and
                registered_attempt['knowledgeDigest'] == canonical_digest(receipt['knowledge']) and
                all(registered_attempt[k] == receipt[k] for k in ('status', 'failureAttribution',
                    'packDigest', 'corpusDigest', 'modelId')), 'receipt differs from retained attempt registry')
        begin, end = timestamp(receipt['startedAt']), timestamp(receipt['completedAt'])
        require(start <= begin <= end <= min(expiry, current), f'{cid}: invalid attempt time window')
        exact_keys(receipt['usage'], {'modelTurns', 'executedActions'}, 'usage')
        for field, limit in (('modelTurns', 'maxTurns'), ('executedActions', 'maxActions')):
            value = receipt['usage'][field]
            require(type(value) is int and 0 <= value <= expected['limits'][limit],
                    f'{cid}: invalid/excess {field}')
            require(receipt['status'] != 'pass' or value > 0, f'{cid}: pass without actual execution')
        require((end - begin).total_seconds() <= expected['limits']['timeoutSeconds'],
                f'{cid}: case exceeded time limit')
        namespace = receipt['namespace']
        require(isinstance(namespace, str) and namespace and namespace not in names,
                f'{cid}: attempt must use a fresh namespace')
        names.add(namespace)
        exact_keys(receipt['costs'], {'modelUsd', 'infrastructureUsd', 'cleanupUsd', 'cleanupReservedUsd'}, 'costs')
        costs = {k: amount(v) for k, v in receipt['costs'].items()}
        require(costs['cleanupReservedUsd'] > 0, f'{cid}: missing cleanup reserve')
        for kind in ('model', 'infrastructure', 'cleanup'):
            key = (cid, receipt['attempt'], kind)
            require(key in settled and settled[key] == costs[kind + 'Usd'],
                    f'{cid}: {kind} cost does not match trusted candidate ledger')
            del settled[key]
        total += sum(costs[k] for k in ('modelUsd', 'infrastructureUsd', 'cleanupUsd'))
        exact_keys(receipt['cleanup'], {'status', 'evidence'}, 'cleanup')
        require(receipt['cleanup']['status'] == 'pass', f'{cid}: cleanup incomplete')
        cleanup = read_json(evidence(evidence_root, receipt['cleanup']['evidence']))
        oracle_predicates.verify('cleanup.complete', cleanup, expectations['cases'][cid]['cleanup'])
        transcript = read_json(evidence(evidence_root, receipt['transcript']))
        exact_keys(transcript, {'campaignId', 'caseId', 'attempt', 'modelId', 'returnedModelId', 'turns', 'actions'},
                   'model transcript')
        for field in ('campaignId', 'caseId', 'attempt', 'modelId', 'returnedModelId'):
            require(canonical_digest(transcript[field]) == canonical_digest(receipt[field]),
                    f'{cid}: transcript belongs to another model/attempt')
        for field, counter in (('turns', 'modelTurns'), ('actions', 'executedActions')):
            require(isinstance(transcript[field], list) and len(transcript[field]) == receipt['usage'][counter],
                    f'{cid}: transcript usage does not match execution')
            require(all(isinstance(row, dict) and row for row in transcript[field]),
                    f'{cid}: transcript contains empty/nonstructured events')
        require(receipt['status'] in ('pass', 'fail', 'blocked'), f'{cid}: skip/unknown outcome')
        if receipt['status'] == 'pass':
            require(receipt['failureAttribution'] is None and receipt['failureEvidence'] is None,
                    f'{cid}: pass carries failure')
            report = read_json(evidence(evidence_root, receipt['oracle']))
            exact_keys(report, {'campaignId', 'caseId', 'attempt', 'componentBindingDigest', 'producer', 'assertions'},
                       'oracle report')
            for key in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest'):
                require(canonical_digest(report[key]) == canonical_digest(receipt[key]),
                        f'{cid}: oracle bound to different attempt')
            require(report['producer'] == 'independent-oracle', f'{cid}: model self-report cannot qualify')
            observed = set()
            require(isinstance(report['assertions'], list), f'{cid}: missing assertions')
            for assertion in report['assertions']:
                exact_keys(assertion, {'id', 'status', 'evidence'}, 'assertion')
                aid = assertion['id']
                require(aid in expected['assertions'] and aid not in observed,
                        f'{cid}: unknown/duplicate assertion')
                observed.add(aid)
                require(assertion['status'] == 'pass', f'{cid}: failed assertion {aid}')
                observation = read_json(evidence(evidence_root, assertion['evidence']))
                oracle_predicates.verify(aid, observation, expectations['cases'][cid]['assertions'][aid])
            require(observed == expected['assertions'], f'{cid}: missing independent assertions')
        else:
            require(receipt['failureAttribution'] in ('infrastructure', 'model', 'product', 'undetermined'),
                    f'{cid}: missing failure attribution')
            evidence(evidence_root, receipt['failureEvidence'])
            require(receipt['oracle'] is None, f'{cid}: failed attempt must not claim passing oracle')
        groups[cid].append(receipt)
    # Reject unlisted receipt files rather than silently cherry-picking an inventory.
    actual = {p.relative_to(receipts_root).as_posix() for p in receipts_root.rglob('*.json') if p.is_file()}
    require(actual == paths, 'unlisted/missing receipt files')
    require(set(groups) == set(cases), f'missing cases: {len(set(cases) - set(groups))}')
    windows = {}
    for target in pack['targets']:
        rows = [r for attempts in groups.values() for r in attempts if r['target'] == target]
        windows[target] = (min(timestamp(r['startedAt']) for r in rows),
                           max(timestamp(r['completedAt']) for r in rows))
    for previous, following in zip(pack['targets'], pack['targets'][1:]):
        require(windows[previous][1] <= windows[following][0], 'target campaigns must run serially in frozen order')
    retries = 0
    for cid, attempts in groups.items():
        attempts.sort(key=lambda r: r['attempt'])
        require(prior_infrastructure_failures[cid] + len(attempts) <= 2,
                f'{cid}: infrastructure retry budget cannot reset across campaigns')
        require([r['attempt'] for r in attempts] in ([1], [1, 2]), f'{cid}: duplicate/missing/excess attempts')
        require(attempts[-1]['status'] == 'pass', f'{cid}: final attempt did not pass')
        if len(attempts) == 2:
            retries += 1
            first, second = attempts
            require(first['status'] in ('fail', 'blocked') and first['failureAttribution'] == 'infrastructure',
                    f'{cid}: model/product failures cannot be retried to green')
            require(timestamp(first['completedAt']) <= timestamp(second['startedAt']),
                    f'{cid}: retry precedes original completion')
    require(total <= amount(campaign['totalBudgetUsd']), 'total campaign cost exceeds $500')
    require(not settled, 'ledger includes missing/cherry-picked campaign attempts')
    return {'schemaVersion': 1, 'status': 'pass', 'campaignId': campaign['campaignId'],
            'packDigest': campaign['packDigest'], 'componentBindingDigest': campaign['componentBindingDigest'],
            'casesExpected': len(cases), 'casesPassed': len(groups), 'attempts': len(paths),
            'infrastructureRetries': retries, 'totalCostUsd': str(total),
            'candidateLiabilityUsd': budget_summary['liabilityUsd']}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('pack', 'campaign', 'lock', 'trusted-inventory', 'receipts', 'evidence-root', 'budget-ledger', 'expectations', 'attempt-registry'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = validate(args.pack, args.campaign, args.lock, args.trusted_inventory,
                          args.receipts, args.evidence_root, args.budget_ledger, args.expectations, args.attempt_registry)
    except (Invalid, OSError, ValueError, KeyError, TypeError) as exc:
        result = {'schemaVersion': 1, 'status': 'fail', 'errors': [str(exc)]}
    output = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    print(output, end='')
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
