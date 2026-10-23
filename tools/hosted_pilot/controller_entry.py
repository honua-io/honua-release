#!/usr/bin/env python3
"""Fixed nonroot entry for a source-verified private controller invocation."""
from __future__ import annotations
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request

from controller_archive import BUNDLE_LIMIT, SOURCE_LIMIT, bundle_bytes, canonical, contract, digest, extract, require, seal_source
from controller_budget import read_allowance, read_version
from controller_handoff import CONFIG_PATH, WORKFLOW, PREPARE_WORKFLOW, PREPARATIONS, bundle_identity, sdk, validate_configuration, put_once, model_runtime

INPUTS = ('campaign.json', 'producer-inputs.json', 'qualification-lock.json', 'expectations.json', 'candidate-provenance.json', 'preparation.json', 'budget-bootstrap-original.json')
FINAL_FILES = {'campaign.json', 'producer-inputs.json', 'qualification-lock.json', 'expectations.json', 'candidate-provenance.json', 'budget-bootstrap-original.json', 'trusted-inventory.json', 'budget-ledger.json', 'attempt-registry.json', 'qualification-summary.json'}


def approved_model_environment(config, campaign, producer, environment):
    pin = config.get('model')
    expected = model_runtime(pin, config['region'], config['accountId'])
    model = producer['model']
    require(all(model[name] == pin[name] for name in ('modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn')) and
            model['profile']['sha256'] == pin['profileSha256'] and
            all(campaign[name] == pin[name] for name in ('modelId', 'returnedModelId')),
            'frozen campaign model differs from installed controller runtime binding')
    require(all(environment.get(name) == value for name, value in expected.items()),
            'private controller model environment is missing or changed')
    return expected


def approved_model_environment(config, campaign, producer, environment):
    pin = config.get('model')
    expected = model_runtime(pin, config['region'], config['accountId'])
    model = producer['model']
    require(all(model[name] == pin[name] for name in ('modelId', 'returnedModelId', 'profileArn', 'tokenCountingModelArn')) and
            model['profile']['sha256'] == pin['profileSha256'] and
            all(campaign[name] == pin[name] for name in ('modelId', 'returnedModelId')),
            'frozen campaign model differs from installed controller runtime binding')
    require(all(environment.get(name) == value for name, value in expected.items()),
            'private controller model environment is missing or changed')
    return expected


def http_json(url, *, limit=1024 * 1024):
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'honua-private-controller'})
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(limit + 1)
    require(len(raw) <= limit, 'native provenance response exceeds bounds')
    return json.loads(raw)


def github_provenance(identity, source):
    """Independent native workflow/source observation; caller fields grant no trust."""
    require(identity['repository'] == 'honua-io/honua-release' and identity['workflow'] in (WORKFLOW, PREPARE_WORKFLOW) and source['sourceSha'] == identity['sourceSha'] and re.fullmatch(r'[a-f0-9]{40}', source['sourceSha']) and type(identity['runId']) is int and identity['runId'] > 0 and type(identity['runAttempt']) is int and identity['runAttempt'] > 0, 'invalid original GitHub execution identity')
    base = 'https://api.github.com/repos/honua-io/honua-release'
    workflow = http_json(base + '/actions/workflows/' + identity['workflow'])
    run = http_json(base + f"/actions/runs/{identity['runId']}/attempts/{identity['runAttempt']}")
    require(run['workflow_id'] == workflow['id'] and run['head_sha'] == source['sourceSha'] and run['head_branch'] == 'trunk' and run['event'] == 'workflow_dispatch' and run['repository']['full_name'] == identity['repository'] and run['run_attempt'] == identity['runAttempt'], 'native GitHub producer differs from frozen campaign')
    branch = http_json(base + '/branches/trunk')
    require(branch['protected'] is True, 'controller source requires protected trunk')
    compare = http_json(base + '/compare/' + source['sourceSha'] + '...trunk')
    require(compare['merge_base_commit']['sha'] == source['sourceSha'] and compare['base_commit']['sha'] == source['sourceSha'] and compare['base_commit']['commit']['tree']['sha'] == source['treeId'], 'controller execution source/tree is not on reviewed protected trunk')
    return {'workflowId': workflow['id'], 'runId': run['id'], 'runAttempt': run['run_attempt'], 'sourceSha': run['head_sha'], 'treeId': source['treeId'], 'observedAt': datetime.now(timezone.utc).isoformat()}


def object_bytes(s3, reference, *, custody, prefix, limit):
    require(set(reference) >= {'bucket', 'key', 'versionId', 'sha256'} and reference['bucket'] == custody['bucket'] and reference['key'].startswith(prefix) and re.fullmatch(r'sha256:[a-f0-9]{64}', reference['sha256']), 'controller object lies outside fixed private custody')
    raw = read_version(s3, reference['bucket'], reference['key'], reference['versionId'], limit=limit)
    require(digest(raw) == reference['sha256'], 'controller immutable object digest differs')
    return raw


def verify_native_task_submission(sm, execution_arn, task_arn):
    def contains(value):
        if isinstance(value, str):
            return value == task_arn
        if isinstance(value, dict):
            return any(contains(item) for item in value.values())
        if isinstance(value, list):
            return any(contains(item) for item in value)
        return False
    for _ in range(10):
        history = sm.get_execution_history(executionArn=execution_arn, reverseOrder=True, maxResults=100, includeExecutionData=True)
        for event in history['events']:
            if event['type'] == 'TaskSubmitted':
                output = json.loads(event['taskSubmittedEventDetails']['output'])
                require(contains(output), 'native lifecycle submitted a different controller task')
                return
        time.sleep(1)
    raise ValueError('native task submission cannot be observed; refuse execution effects')


def file_manifest(root):
    return [{'path': path.relative_to(root).as_posix(), 'size': path.stat().st_size, 'sha256': digest(path.read_bytes())}
            for path in sorted(Path(root).rglob('*')) if path.is_file()]


def retain_archive(s3, config, invocation, root, kind, *, immutable_inputs):
    custody = config['custody']
    raw = bundle_bytes(root)
    archive = put_once(s3, custody['bucket'], custody['evidencePrefix'] + invocation + '/' + kind + '-' + digest(raw)[7:] + '.tar', raw)
    manifest = {'schema': 'honua.private-controller-artifacts/v1', 'kind': kind, 'invocationId': invocation,
        'archive': archive, 'immutableInputs': immutable_inputs, 'files': file_manifest(root)}
    manifest_raw = canonical(manifest)
    require(len(manifest_raw) <= 4 * 1024 * 1024, 'controller artifact manifest exceeds bounds')
    manifest_ref = put_once(s3, custody['bucket'], custody['evidencePrefix'] + invocation + '/' + kind + '-manifest-' + digest(manifest_raw)[7:] + '.json', manifest_raw)
    return {'archive': archive, 'manifest': manifest_ref}


def main():
    require(os.getuid() == 10001, 'private controller must run as its reviewed nonroot identity')
    binding = json.loads(os.environ['PILOT_CONTROLLER_CUSTODY'])
    require(set(binding) == {'region', 'accountId', 'bucket', 'requestPrefix'} and re.fullmatch(r'[0-9]{12}', binding['accountId']), 'fixed controller task custody binding required')
    pointer = json.loads(os.environ['PILOT_CONTROLLER_REQUEST'])
    s3 = sdk('s3', binding['region'])
    raw = object_bytes(s3, pointer, custody=binding, prefix=binding['requestPrefix'], limit=65536)
    request = json.loads(raw)
    require(request['schema'] == 'honua.private-controller-request/v1', 'unsupported private controller request')
    config, invocation = request['configuration'], request['invocationId']
    validate_configuration(config)
    require(config['region'] == binding['region'] and config['accountId'] == binding['accountId'] and config['custody']['bucket'] == binding['bucket'] and config['custody']['requestPrefix'] == binding['requestPrefix'] and digest(canonical(config)) == request['configurationDigest'] and digest(canonical(request['invocation']))[7:] == invocation and request['image'] == config['image'], 'private controller request identity differs')
    require(sdk('sts', binding['region']).get_caller_identity()['Account'] == binding['accountId'], 'private controller AWS account differs')
    # Verify the sealed execution-environment image independently of the newer
    # source archive. Runtime contract changes require a new environment image.
    image_receipt = json.loads(Path('/opt/honua-controller-source.json').read_text())
    require(image_receipt['source_sha'] == config['image']['sourceSha'] and image_receipt['tree_id'] == config['image']['treeId'] and image_receipt['archive_sha256'] == config['image']['archiveSha256'] and contract('/controller') == config['image']['runtimeContractSha256'], 'private controller environment image source/dependency contract differs')
    provenance = github_provenance(request['invocation']['controller'], request['source'])
    execution_arn = config['lifecycle']['stateMachineArn'].replace(':stateMachine:', ':execution:').rsplit(':', 1)[0] + ':' + invocation
    sm = sdk('stepfunctions', binding['region'])
    observed = sm.describe_execution(executionArn=execution_arn)
    require(observed['stateMachineArn'] == config['lifecycle']['stateMachineArn'] and observed['status'] == 'RUNNING' and observed['input'] == canonical({'request': canonical(pointer).decode()}).decode() and observed.get('redriveCount', 0) == 0, 'private controller is not its original native no-redrive execution')
    metadata_uri = os.environ['ECS_CONTAINER_METADATA_URI_V4']
    require(re.fullmatch(r'http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+', metadata_uri), 'native ECS metadata endpoint differs')
    metadata = http_json(metadata_uri + '/task', limit=65536)
    task_arn = metadata['TaskARN']
    verify_native_task_submission(sm, execution_arn, task_arn)
    require(metadata['Cluster'] == config['clusterArn'] or metadata['Cluster'] == config['clusterArn'].split('/')[-1], 'native private controller cluster differs')
    ecs = sdk('ecs', binding['region'])
    task_response = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
    require(not task_response.get('failures') and len(task_response.get('tasks', [])) == 1, 'private controller native task cannot be observed')
    task = task_response['tasks'][0]
    require(task['taskDefinitionArn'] == config['taskDefinitionArn'] and len(task['containers']) == 1 and task['containers'][0]['image'] == config['image']['uri'] and task.get('enableExecuteCommand') is False, 'private controller task/image execution identity differs')
    db = sdk('dynamodb', binding['region'])
    read_allowance(config['dispatchBudget'], s3, db)
    claim = db.get_item(TableName=config['dispatchBudget']['claimTable'], Key={'claimKey': {'S': 'dispatch:' + invocation}}, ConsistentRead=True).get('Item', {})
    require(claim.get('bindingDigest', {}).get('S') == digest(raw) and claim.get('executionArn', {}).get('S') == execution_arn, 'private controller lacks retained dispatch capacity claim')
    source_raw = object_bytes(s3, request['source'], custody=config['custody'], prefix=config['custody']['sourcePrefix'], limit=SOURCE_LIMIT)
    bundle_raw = object_bytes(s3, request['bundle'], custody=config['custody'], prefix=config['custody']['bundlePrefix'], limit=BUNDLE_LIMIT)
    source = extract(source_raw, '/work/release', limit=SOURCE_LIMIT)
    bundle = extract(bundle_raw, '/work/campaign', limit=BUNDLE_LIMIT)
    require(json.loads((source / CONFIG_PATH).read_text()) == config and contract(source) == config['image']['runtimeContractSha256'], 'downloaded execution source changes reviewed config or fixed entry/dependencies')
    seal_source(source, request['source']['treeId'])
    selected = request['invocation']
    campaign, expected, state = bundle_identity(bundle, operation=selected['operation'], case_id=selected['caseId'], target=selected['target'], shard=selected['shard'], shards=selected['shards'])
    require(selected == expected and state == config['dispatchBudget']['state'], 'restored bundle differs from exact invocation/state')
    immutable_inputs = {name: digest((bundle / name).read_bytes()) for name in INPUTS if (bundle / name).is_file()}
    required = {'preparation.json'} if selected['operation'] in PREPARATIONS else {'campaign.json', 'producer-inputs.json', 'qualification-lock.json', 'expectations.json'}
    require(required <= set(immutable_inputs), 'private frozen inputs are incomplete')
    evidence = Path('/work/controller-evidence')
    evidence.mkdir(mode=0o700)
    (evidence / 'native-provenance.json').write_bytes(canonical(provenance))
    command = [sys.executable, 'tools/agent_e2e_campaign.py', selected['operation'], '--bundle', str(bundle)]
    if selected['operation'] in PREPARATIONS:
        command = [sys.executable, 'tools/hosted_pilot/prepare.py', 'execute', '--bundle', str(bundle), '--output', str(evidence/'preparation-result.json')]
    if selected['caseId']:
        command += ['--case-id', selected['caseId']]
    if selected['target']:
        command += ['--target', selected['target'], '--shard', str(selected['shard']), '--shards', str(selected['shards'])]
    env = os.environ.copy()
    if selected['operation'] not in PREPARATIONS:
        env.update(approved_model_environment(config, campaign,
            json.loads((bundle/'producer-inputs.json').read_text()), env))
    producer = campaign['controller']
    env.update(GITHUB_REPOSITORY=producer['repository'], GITHUB_REF='refs/heads/trunk', GITHUB_EVENT_NAME='workflow_dispatch', GITHUB_SHA=producer['sourceSha'], GITHUB_RUN_ID=str(producer['runId']), GITHUB_RUN_ATTEMPT=str(producer['runAttempt']), PYTHONDONTWRITEBYTECODE='1', PGSSLROOTCERT='/controller/certification/hosted-pilot/controller/rds-global-bundle.pem')
    env['PILOT_CONTROLLER_TASK_ARN'] = task_arn
    started = datetime.now(timezone.utc).isoformat()
    # The task lifetime includes image pull/startup; source work gets only the
    # remaining original native execution deadline, never a fresh full timeout.
    remaining = config['maxSeconds'] - (datetime.now(timezone.utc) - observed['startDate']).total_seconds() - 45
    require(remaining >= 30, 'private controller native deadline is exhausted before execution')
    log = evidence / 'producer.log'
    with log.open('wb') as output:
        process = subprocess.Popen(command, cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        deadline = time.monotonic() + remaining
        while process.poll() is None:
            if time.monotonic() >= deadline or log.stat().st_size > 16 * 1024 * 1024:
                import signal
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                break
            time.sleep(1)
    exit_code = process.returncode if 0 <= process.returncode <= 255 else 1
    require(all((bundle / name).is_file() and digest((bundle / name).read_bytes()) == value for name, value in immutable_inputs.items()), 'private producer mutated its immutable frozen inputs')
    artifacts = {'evidence': retain_archive(s3, config, invocation, evidence, 'private-evidence', immutable_inputs=immutable_inputs)}
    if selected['operation'] == 'finalize' and exit_code == 0:
        final = Path('/work/finalized')
        final.mkdir(mode=0o700)
        import shutil
        for name in FINAL_FILES:
            if (bundle / name).is_file():
                shutil.copyfile(bundle / name, final / name)
        for name in ('receipts', 'evidence'):
            if (bundle / name).exists():
                shutil.copytree(bundle / name, final / name, symlinks=True)
        artifacts['bundle'] = retain_archive(s3, config, invocation, final, 'finalized-bundle', immutable_inputs=immutable_inputs)
    outcome = {'schema': 'honua.private-controller-outcome/v1', 'status': 'completed' if exit_code == 0 else 'failed', 'exitCode': exit_code,
        'invocationId': invocation, 'invocation': selected, 'requestDigest': digest(raw), 'executionArn': execution_arn, 'taskArn': task_arn,
        'configurationDigest': request['configurationDigest'], 'source': request['source'], 'image': config['image'],
        'startedAt': started, 'completedAt': datetime.now(timezone.utc).isoformat(), 'artifacts': artifacts}
    put_once(s3, config['custody']['bucket'], config['custody']['outcomePrefix'] + invocation + '.json', canonical(outcome))
    # Native transport success carries the actual CLI exit in the retained
    # outcome. The dispatcher propagates it after custody validation.
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        print('private controller refused; retain native task/execution and reserved liability', file=sys.stderr)
        sys.exit(1)
