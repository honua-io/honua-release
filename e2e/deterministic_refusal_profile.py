"""Bounded deterministic serving/refusal proof; the eight-stage receipt stays unchanged.

Shared live operations and fixture oracles belong to the maintained terminal driver.
This adapter supplies fixed harness actions, never genuine-model evidence or GP completion.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PROFILE = 'redis-off-serving-refusal'
CELLS = {'aws-ecs/redis-off', 'aws-serverless/redis-off'}
ASSERTIONS = {'servingRead', 'typedRedisRefusal', 'authentication', 'anonymousDenial', 'viewerDenial', 'catalogExact'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def instant(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(result.tzinfo is not None, 'profile timestamp needs timezone')
    return result


def oracle():
    # Only the reviewed repository helper is eligible; no operator-selected Python path.
    path = ROOT / 'certification/terminal-journey'
    sys.path.insert(0, str(path))
    try:
        module = importlib.import_module('refusal_profile')
        require(Path(module.__file__).resolve() == path / 'refusal_profile.py', 'unexpected serving oracle source')
        return module
    finally:
        sys.path.pop(0)


def plan(target, run_id, number):
    spec = target['execution']['buffer']
    x, y = spec['point']
    wkb = base64.b64encode(struct.pack('<BI2d', 1, 1, x, y)).decode()
    return {'plan': {'planId': f'off-plan-{run_id}-{number}', 'intentId': f'off-intent-{run_id}-{number}',
        'steps': [{'stepId': 'buffer', 'kind': 'Geoprocess', 'processId': 'geometry.buffer',
                   'inputs': {'wkb': wkb, 'srid': str(spec['srid']), 'distance': str(spec['distance']), 'geodesic': 'false'}}],
        'outputs': ['FeatureLayer']}}


def no_execution(value):
    if isinstance(value, dict):
        return all(not (key in {'jobId', 'operationId', 'operationInstanceId', 'jobStatus', 'resourceUri', 'jobCreatedAt'} and item)
                   and no_execution(item) for key, item in value.items())
    if isinstance(value, list):
        return all(no_execution(item) for item in value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return True
        return no_execution(parsed) if isinstance(parsed, (dict, list)) else True
    return True


def check_refusal(value):
    require(isinstance(value, dict) and no_execution(value)
        and value.get('topology') == 'redis-off' and value.get('rulingStatus') == 'ruled'
        and value.get('tool') == 'honua_execute_plan' and value.get('code') == 'unavailable'
        and value.get('missingDependency') == 'redis' and value.get('manifestCapability') == 'jobs.runner'
        and value.get('manifestReasonCode') == 'dependency-unavailable', 'profile lacks actual typed durable GP refusal')


def produce(record, raw_path, target_path, *, adapter, pinned, digest, cell):
    """Fixed actions through the same pinned driver, in the existing credential-free job."""
    from tools.terminal_model_canary import validate_deterministic_receipt, load_journey
    require(cell in CELLS, 'serving/refusal profile is only for approved Redis-off cells')
    raw_bytes = raw_path.read_bytes()
    raw = json.loads(raw_bytes)
    fixture_path = target_path.parent / 'extended/seed-manifest.json'
    shared = oracle()
    fixture = shared.fixture_binding(fixture_path)
    target = json.loads(target_path.read_bytes())
    args = plan(target, record['runId'], record['number'])
    shared.validate_durable_plan(args, target['execution'])
    prerequisite = validate_deterministic_receipt(raw_path, manifest=pinned,
        journey=load_journey(ROOT / 'certification/terminal-journey/journey.v1.json'),
        cell=cell, prerequisite_profile=PROFILE)
    result = {'schemaVersion': 'deterministic-redis-off-serving-refusal.v1', 'profile': PROFILE,
        'mode': 'deterministic', 'cell': cell, 'runId': record['runId'], 'runAttempt': record['runAttempt'],
        'attempt': record['number'], 'candidateDigest': digest, 'server': raw['server'],
        'rawReceiptSha256': sha(raw_bytes), 'targetConfigSha256': sha(target_path.read_bytes()),
        'sourceStatus': raw['status'], 'planSha256': sha(canonical(args)), 'status': 'fail',
        'executionOutcome': 'not-attempted', 'functionalGpExecuted': None, 'cleanup': 'not-attempted'}
    workspace = None
    try:
        setup = adapter.op_setup({'target': str(target_path), 'profile': PROFILE,
            'deploymentReceipt': prerequisite,
            'servingFixture': {'path': str(fixture_path), 'sha256': sha(fixture_path.read_bytes())}})
        workspace = setup.get('workspaceId')
        require(setup.get('status') == 'ready' and bool(workspace), 'profile driver setup unavailable')
        common = {'workspaceId': workspace, 'profile': PROFILE}
        def execute(stage, name, arguments, **extra):
            answer = adapter.op_execute({**common, 'stage': stage,
                'action': {'kind': 'tool_call', 'tool': name, 'arguments': arguments, **extra}})
            require(answer.get('status') == 'pass', 'profile live operation did not pass')
            return answer.get('result', {})
        execute('serving-read', 'honua_query_features', {'serviceId': fixture['serviceId'],
            'layerId': fixture['layerId'], 'where': '1=1', 'outFields': ['*'], 'outSrid': 4326,
            'returnGeometry': True, 'limit': 100, 'geometryPrecision': 6})
        refusal = execute('durable-refusal', 'honua_execute_plan', args)
        require(refusal.get('accepted') is False and refusal.get('executionOutcome') == 'refused'
                and refusal.get('functionalGpExecuted') is False, 'durable request was not refused')
        check_refusal(refusal.get('typedRefusal'))
        execute('durable-refusal', 'honua_list_capabilities', {}, assessment={
            'executionOutcome': 'refused', 'missingDependency': 'redis', 'functionalGpExecuted': False})
        verified = adapter.op_verify(common)
        require(verified.get('status') == 'pass' and verified.get('executionOutcome') == 'refused'
            and verified.get('functionalGpExecuted') is False
            and all(verified.get('assertions', {}).get(key) == 'pass' for key in ASSERTIONS),
            'profile independent security/serving checks did not pass')
        check_refusal(verified.get('typedRefusal'))
        result.update(status='pass', executionOutcome='refused', functionalGpExecuted=False,
            assertions={key: verified['assertions'][key] for key in sorted(ASSERTIONS)},
            servingProof=verified['servingProof'],
            refusalProof={key: value for key, value in verified['typedRefusal'].items() if key != 'message'})
    except Exception as exc:
        result.update(status='fail', why='profile execution failed: ' + type(exc).__name__)
    finally:
        if workspace:
            try:
                result['cleanup'] = adapter.op_teardown({'workspaceId': workspace}).get('status')
            except Exception:
                result['cleanup'] = 'fail'
        if result['cleanup'] != 'pass':
            result.update(status='fail', why='profile run-owned principal cleanup incomplete')
        result['generatedAt'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return result


def validate(value, record, raw, raw_bytes, target_path, *, cell, digest, expected_server, at=None, require_observation=True):
    """Re-derive bounded admission from retained source/target/fixture and actual operation proof."""
    at = at or datetime.now(timezone.utc)
    require(isinstance(value, dict) and isinstance(raw, dict) and isinstance(record, dict), 'malformed profile envelope')
    require(cell in CELLS and value.get('schemaVersion') == 'deterministic-redis-off-serving-refusal.v1'
        and value.get('profile') == PROFILE and value.get('mode') == 'deterministic'
        and value.get('status') == 'pass' and value.get('executionOutcome') == 'refused'
        and value.get('functionalGpExecuted') is False and value.get('cleanup') == 'pass',
        'invalid deterministic serving/refusal profile')
    contract = json.loads((ROOT / 'certification/terminal-journey/journey.v1.json').read_bytes())
    expected_stages = [(s['number'], s['id'], s['command']) for s in contract['stages']]
    require(isinstance(raw.get('stages'), list) and all(isinstance(s, dict) for s in raw['stages'])
        and [(s.get('number'), s.get('stage'), s.get('command')) for s in raw['stages']] == expected_stages,
        'profile source does not cover the complete ordered canonical journey')
    require(value.get('cell') == record['cell'] == cell and value.get('attempt') == record['number']
        and value.get('runId') == record['runId'] and value.get('runAttempt') == record['runAttempt']
        and value.get('candidateDigest') == record['candidateDigest'] == digest
        and record.get('driver', 'deterministic') == 'deterministic'
        and value.get('server') == raw.get('server') == expected_server
        and raw.get('mode') == 'live' and raw.get('roster', {}).get('status') == 'pass'
        and raw.get('status') in {'pass', 'blocked'} and value.get('sourceStatus') == raw['status']
        and not any(stage.get('status') == 'fail' for stage in raw.get('stages', [])), 'wrong profile source/candidate binding')
    require(value.get('rawReceiptSha256') == record['receiptSha256'] == sha(raw_bytes)
        and value.get('targetConfigSha256') == raw['target'].get('configSha256') == sha(target_path.read_bytes()),
        'profile raw receipt/target integrity mismatch')
    target = json.loads(target_path.read_bytes())
    suffix = f"#honua-run={record['runId']}/{record['runAttempt']}"
    require(all(stage['evidence']['uri'].endswith(suffix) and stage['evidence']['uri'].count('#') == 1
                for stage in raw['stages']), 'profile raw evidence belongs to another job attempt')
    for stage in raw['stages'][:2]:
        evidence = stage['evidence']
        require(stage['status'] == 'pass' and evidence.get('source') == 'live-' + cell.split('/')[0]
            and evidence.get('freshness') == 'verified-current' and evidence.get('completeness') == 'complete'
            and 0 <= (instant(raw['generatedAt']) - instant(evidence['observedAt'])).total_seconds() <= 86400,
            'profile setup/auth prerequisite is not current complete live evidence')
    authored = json.loads((ROOT / 'certification/terminal-journey/targets/local-docker.json').read_bytes())
    require(target.get('execution', {}).get('buffer') == authored['execution']['buffer'],
            'profile buffer input is not the authored candidate fixture')
    require(no_execution(raw['stages'][4]), 'raw GP stage contradicts refused execution')
    require(target.get('id') == cell and target.get('kind') == cell.split('/')[0]
        and str(target.get('replicaBaseUrl', '')).rstrip('/') == str(raw['target'].get('baseUrl', '')).rstrip('/'),
        'profile target endpoint mismatch')
    generated = instant(value['generatedAt'])
    require(instant(raw['generatedAt']) <= generated <= at and (at - generated).total_seconds() <= 86400,
            'profile evidence is stale, future or precedes the source receipt')
    require(value.get('planSha256') == sha(canonical(plan(target, record['runId'], record['number']))),
            'profile durable attempt differs from authored valid fixture input')
    expected = oracle().fixture_binding(target_path.parent / 'extended/seed-manifest.json')
    public = {key: item for key, item in expected.items() if key != 'expectedFeatures'}
    serving = value.get('servingProof', {})
    require(serving.get('fixtureBinding') == public and serving.get('featureCount') == len(expected['expectedFeatures'])
        and serving.get('featureIds') == sorted(feature['id'] for feature in expected['expectedFeatures'])
        and serving.get('independentReadMatches') is True, 'serving proof differs from authored candidate fixture')
    require(value.get('assertions') == dict.fromkeys(ASSERTIONS, 'pass'), 'profile security/catalog assertion missing')
    check_refusal(value.get('refusalProof'))
    if require_observation:
        observation = record.get('profileObservation', {})
        require(observation.get('source') == 'aws-control-plane'
            and observation.get('before') == observation.get('after') == expected_server
            and observation.get('seedManifestSha256') == public['seedManifestSha256']
            and generated <= instant(observation['observedAt']) <= at,
            'profile lacks matching native image observations before and after execution')
    return {'profile': PROFILE, 'executionOutcome': 'refused', 'functionalGpExecuted': False,
            'completedAt': value['generatedAt']}


def validate_record(record, raw, raw_path, *, cell, pinned, digest, at=None, require_observation=True):
    """Only fixed adjacent hashed sidecars are eligible; raw receipts remain untouched."""
    path = raw_path.parent / f"serving-refusal-{record['number']}.json"
    proof = record.get('servingRefusal', {})
    directory = raw_path.parent.resolve()
    for file in (raw_path, path, raw_path.parent / f"target-{record['number']}.json",
                 raw_path.parent / 'extended/seed-manifest.json'):
        require(file.is_file() and not file.is_symlink() and file.resolve().is_relative_to(directory),
                'unsafe or missing serving/refusal artifact')
    require(isinstance(proof, dict) and proof.get('file') == path.name and proof.get('sha256') == sha(path.read_bytes()),
            'missing or changed serving/refusal sidecar')
    from cloud_journey import candidate_image
    server = {'sourceSha': pinned['components']['honua-server']['sha'], 'image': candidate_image(cell, pinned)}
    return validate(json.loads(path.read_bytes()), record, raw, raw_path.read_bytes(),
        raw_path.parent / f"target-{record['number']}.json", cell=cell, digest=digest,
        expected_server=server, at=at, require_observation=require_observation)


def failed_record(record, raw, raw_path):
    """A failed attempt remains attributed history; it cannot satisfy a required success."""
    path = raw_path.parent / f"serving-refusal-{record['number']}.json"
    if raw.get('status') != 'pass' and not record.get('servingRefusal'):
        require(not path.exists(), 'produced serving/refusal sidecar omitted from attempt history')
        return True
    proof = record.get('servingRefusal', {})
    require(isinstance(proof, dict) and proof.get('file') == path.name
        and path.is_file() and not path.is_symlink()
        and proof.get('sha256') == sha(path.read_bytes()), 'missing or changed serving/refusal sidecar')
    return json.loads(path.read_bytes()).get('status') == 'fail'
