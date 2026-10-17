"""Negative controls for selected retained lock and independently observed ECS identity."""
import copy
import hashlib
import json
from pathlib import Path
import sys

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import demo_canary_binding as binding

A = 'sha256:' + 'a' * 64
B = 'sha256:' + 'b' * 64
SHA = 'a' * 40
BASE = 'https://demo.honua.io'


class GitHub:
    def __init__(self, workflow='.github/workflows/release-train.yml'):
        self.run = {'id': 101, 'repository': {'full_name': binding.REPOSITORY},
                    'head_repository': {'full_name': binding.REPOSITORY}, 'head_branch': 'trunk',
                    'path': workflow, 'event': 'schedule', 'status': 'completed', 'conclusion': 'success', 'head_sha': SHA}
        self.repo = {'full_name': binding.REPOSITORY, 'default_branch': 'trunk'}
        self.branch = {'protected': True}
        self.lock = {'platform': {'id': 'honua-2026.1-rc.3'},
                     'components': {'honua-server': {'source': {'revision': SHA}}}}
        self.available = True

    def api(self, path=''):
        return self.run if path.startswith('actions/runs/') else self.branch if path == 'branches/trunk' else self.repo

    def download(self, run_id, artifact, root):
        assert run_id == '101' and artifact == 'certified-candidate'
        if self.available:
            (root / 'platform-lock.json').write_text(json.dumps(self.lock))
        return self.available


def prepared(tmp_path, monkeypatch, *, workflow='.github/workflows/release-train.yml'):
    monkeypatch.chdir(tmp_path)
    record = Path('certification/promotions/2026.1-rc.3.json')
    record.parent.mkdir(parents=True)
    gh = GitHub(workflow)
    digest = 'sha256:' + hashlib.sha256(json.dumps(gh.lock).encode()).hexdigest()
    record.write_text(json.dumps({'platformLabel': '2026.1-rc.3', 'rcTrainRunId': '101', 'lock': {'digest': digest}}))
    commands = []
    def run(args):
        commands.append(args)
        return str(record) if args[0] == 'git' else ''
    return record, tmp_path / 'candidate', gh, run, commands


@pytest.mark.parametrize('workflow', binding.MINTING_WORKFLOWS)
def test_only_authenticated_retained_lock_is_selected(tmp_path, monkeypatch, workflow):
    record, root, gh, run, commands = prepared(tmp_path, monkeypatch, workflow=workflow)
    result = binding.prepare(record, root, gh=gh, run=run, ref='refs/heads/trunk')
    assert result['serverSha'] == SHA and result['runId'] == '101'
    verify = commands[1]
    assert verify[:3] == (['gh', 'attestation', 'verify'] if 'release-train' in workflow else ['cosign', 'verify-blob', str(root / 'platform-lock.json')])
    assert SHA in verify and 'refs/heads/trunk' in verify
    assert '--bundle' in verify and str(root / 'platform-lock.sigstore.json') in verify
    assert '--check' in commands[2] and 'tools/platform_lock_bundle.py' in commands[2]


@pytest.mark.parametrize('mutation', ['branch', 'protection', 'foreign-repo', 'foreign-head', 'failed-mint', 'workflow',
                                      'event', 'digest', 'label', 'untracked', 'missing-artifact', 'signature', 'derived-bundle', 'symlink'])
def test_prepare_refuses_untrusted_selection(tmp_path, monkeypatch, mutation):
    record, root, gh, run, commands = prepared(tmp_path, monkeypatch)
    ref = 'refs/heads/feature' if mutation == 'branch' else 'refs/heads/trunk'
    if mutation == 'protection': gh.branch['protected'] = False
    if mutation == 'foreign-repo': gh.repo['full_name'] = 'other/repo'
    if mutation == 'foreign-head': gh.run['head_repository']['full_name'] = 'other/repo'
    if mutation == 'failed-mint': gh.run['conclusion'] = 'failure'
    if mutation == 'workflow': gh.run['path'] = '.github/workflows/demo-canary.yml'
    if mutation == 'event': gh.run['event'] = 'pull_request'
    if mutation == 'missing-artifact': gh.available = False
    if mutation == 'digest': gh.lock['components']['honua-server']['source']['revision'] = 'b' * 40
    if mutation == 'label': gh.lock['platform']['id'] = 'honua-2026.1-rc.4'
    if mutation == 'symlink':
        source = tmp_path / 'record.json'; source.write_bytes(record.read_bytes()); record.unlink(); record.symlink_to(source)
    original = run
    def run(args):
        if mutation == 'untracked' and args[0] == 'git': return ''
        if mutation == 'signature' and args[0] == 'gh': raise binding.FetchError('bad signature')
        if mutation == 'derived-bundle' and args[0] == 'python': raise binding.FetchError('bad derived bundle')
        return original(args)
    with pytest.raises(binding.FetchError): binding.prepare(record, root, gh=gh, run=run, ref=ref)


def deployment(tmp_path):
    manifest = {'components': {'honua-server': {'image': 'ghcr.io/honua-io/honua-server:2026.1-rc', 'digest': A,
                                               'platformDigests': {'amd64': B}}}}
    (tmp_path / 'platform-manifest.yaml').write_text(yaml.safe_dump(manifest))
    config = dict(region='us-east-1', cluster='demo', service='server', container='honua', zone='Z123')
    data = {
        'describe-services': {'services': [{'serviceArn': 'arn:service', 'status': 'ACTIVE', 'desiredCount': 1,
            'runningCount': 1, 'pendingCount': 0, 'deployments': [{'status': 'PRIMARY', 'rolloutState': 'COMPLETED'}],
            'taskDefinition': 'arn:definition:12', 'loadBalancers': [{'containerName': 'honua', 'containerPort': 8080, 'targetGroupArn': 'arn:tg'}]}]},
        'describe-target-groups': {'TargetGroups': [{'TargetType': 'ip', 'LoadBalancerArns': ['arn:lb']}]},
        'describe-load-balancers': {'LoadBalancers': [{'State': {'Code': 'active'}, 'Scheme': 'internet-facing',
                                                       'DNSName': 'demo.elb.amazonaws.com', 'CanonicalHostedZoneId': 'ZALB'}]},
        'list-resource-record-sets': {'ResourceRecordSets': [{'Name': 'demo.honua.io.', 'Type': 'A',
             'AliasTarget': {'DNSName': 'dualstack.demo.elb.amazonaws.com.', 'HostedZoneId': 'ZALB'}}]},
        'describe-listeners': {'Listeners': [{'Port': 443, 'Protocol': 'HTTPS', 'ListenerArn': 'arn:listener'}]},
        'describe-rules': {'Rules': [{'Actions': [{'Type': 'forward', 'TargetGroupArn': 'arn:tg'}]}]},
        'list-tasks': {'taskArns': ['arn:task']},
        'describe-tasks': {'tasks': [{'taskArn': 'arn:task', 'lastStatus': 'RUNNING', 'taskDefinitionArn': 'arn:definition:12',
             'containers': [{'name': 'honua', 'lastStatus': 'RUNNING', 'image': 'ghcr.io/honua-io/honua-server@' + A, 'imageDigest': B}],
             'attachments': [{'type': 'ElasticNetworkInterface', 'details': [{'name': 'privateIPv4Address', 'value': '10.0.0.1'}]}]}]},
        'describe-target-health': {'TargetHealthDescriptions': [{'Target': {'Id': '10.0.0.1', 'Port': 8080}, 'TargetHealth': {'State': 'healthy'}}]},
    }
    return config, data


def test_observed_ecs_accepts_bound_platform_child_digest(tmp_path):
    config, data = deployment(tmp_path)
    calls = []
    def aws(service, command, *args): calls.append((service, command, args)); return copy.deepcopy(data[command])
    result = binding.observe(tmp_path, BASE, config, aws=aws, resolve=lambda host: {'1.2.3.4'})
    assert result['imageDigest'] == A and result['taskArns'] == ['arn:task']
    assert all(command.startswith(('describe-', 'list-')) for _, command, _ in calls)


@pytest.mark.parametrize('mutation', ['config', 'endpoint', 'desired', 'pending', 'rolling', 'container', 'target-type',
    'alias', 'alias-zone', 'weighted', 'ipv6', 'dns', 'listener', 'rule', 'unobserved-task', 'task-definition',
    'wrong-image', 'wrong-repository', 'stopped', 'network', 'target-ip', 'target-health'])
def test_deployment_identity_refuses_mismatch(tmp_path, mutation):
    config, data = deployment(tmp_path)
    service = data['describe-services']['services'][0]
    task = data['describe-tasks']['tasks'][0]
    record = data['list-resource-record-sets']['ResourceRecordSets'][0]
    base = BASE
    if mutation == 'config': config['cluster'] = ''
    if mutation == 'endpoint': base = 'https://other.test/path'
    if mutation == 'desired': service['desiredCount'] = 0
    if mutation == 'pending': service['pendingCount'] = 1
    if mutation == 'rolling': service['deployments'].append(copy.deepcopy(service['deployments'][0]))
    if mutation == 'container': service['loadBalancers'][0]['containerName'] = 'other'
    if mutation == 'target-type': data['describe-target-groups']['TargetGroups'][0]['TargetType'] = 'instance'
    if mutation == 'alias': record['AliasTarget']['DNSName'] = 'other.elb.amazonaws.com'
    if mutation == 'alias-zone': record['AliasTarget']['HostedZoneId'] = 'OTHER'
    if mutation == 'weighted': record['SetIdentifier'] = 'blue'
    if mutation == 'ipv6': data['list-resource-record-sets']['ResourceRecordSets'].append(dict(Name='demo.honua.io.', Type='AAAA', AliasTarget={'DNSName': 'other'}))
    if mutation == 'listener': data['describe-listeners']['Listeners'][0]['Port'] = 80
    if mutation == 'rule': data['describe-rules']['Rules'][0]['Actions'][0]['TargetGroupArn'] = 'other'
    if mutation == 'unobserved-task': data['describe-tasks']['tasks'] = []
    if mutation == 'task-definition': task['taskDefinitionArn'] = 'old'
    if mutation == 'wrong-image': task['containers'][0]['imageDigest'] = 'sha256:' + 'f' * 64
    if mutation == 'wrong-repository': task['containers'][0]['image'] = 'other/repo@' + A
    if mutation == 'stopped': task['containers'][0]['lastStatus'] = 'STOPPED'
    if mutation == 'network': task['attachments'] = []
    if mutation == 'target-ip': data['describe-target-health']['TargetHealthDescriptions'][0]['Target']['Id'] = '10.0.0.2'
    if mutation == 'target-health': data['describe-target-health']['TargetHealthDescriptions'][0]['TargetHealth']['State'] = 'unhealthy'
    with pytest.raises(binding.FetchError):
        binding.observe(tmp_path, base, config, aws=lambda service, command, *args: data[command],
                        resolve=lambda host: {'1.2.3.4'} if mutation != 'dns' or host == 'demo.honua.io' else {'5.6.7.8'})


def test_workflow_has_daily_monitoring_and_optional_six_hour_burn_without_arbitrary_lock_input():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/demo-canary.yml').read_text())
    triggers = workflow[True]
    assert [r['cron'] for r in triggers['schedule']] == ['23 6 * * *', '23 0,12,18 * * *']
    assert set(triggers['workflow_dispatch']['inputs']) == {'base_url'}
    job = workflow['jobs']['canary']
    assert "vars.DEMO_CANARY_PROMOTION_RECORD != ''" in job['if']
    steps = job['steps']
    prepare = next(i for i, s in enumerate(steps) if 'demo_canary_binding.py prepare' in s.get('run', ''))
    credentials = next(i for i, s in enumerate(steps) if s.get('uses', '').startswith('aws-actions/configure-aws-credentials'))
    assert prepare < credentials
    commands = '\n'.join(s.get('run', '') for s in steps)
    assert 'demo_canary_binding.py observe' in commands and 'demo_canary_binding.py bind' in commands
    assert '--candidate-sha "$HONUA_CANARY_CANDIDATE_SHA"' in commands
    assert 'lock_path = pathlib.Path("platform-lock.json")' not in commands


@pytest.mark.parametrize('mutation', ['none', 'deployment-changed', 'source-sha', 'endpoint'])
@pytest.mark.parametrize('status', ['pass', 'blocked', 'fail'])
def test_final_binding_reobserves_real_identity_and_preserves_probe_verdict(tmp_path, monkeypatch, mutation, status):
    config, data = deployment(tmp_path)
    aws = lambda service, command, *args: data[command]
    resolve = lambda host: {'1.2.3.4'}
    before = binding.observe(tmp_path, BASE, config, aws=aws, resolve=resolve)
    (tmp_path / 'deployment-before.json').write_text(json.dumps(before))
    (tmp_path / 'binding.json').write_text(json.dumps(dict(serverSha=SHA, record='certification/promotions/2026.1-rc.3.json', digest=A, runId='101')))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('GITHUB_RUN_ID', '202')
    e2e = tmp_path / 'e2e'; e2e.mkdir()
    envelope = {'candidateServerSha': SHA, 'targetUrl': BASE}
    if mutation == 'source-sha': envelope['candidateServerSha'] = 'b' * 40
    if mutation == 'endpoint': envelope['targetUrl'] = 'https://other.test'
    if mutation == 'deployment-changed': data['describe-services']['services'][0]['serviceArn'] = 'arn:replaced'
    (e2e / 'live-canary-evidence.json').write_text(json.dumps(envelope))
    (e2e / 'gate-report-demo-canary.json').write_text(json.dumps({'status': status}))
    if mutation == 'none':
        if status == 'pass':
            binding.bind(tmp_path, BASE, config, aws=aws, resolve=resolve)
        else:
            with pytest.raises(binding.FetchError, match='burn is interrupted'):
                binding.bind(tmp_path, BASE, config, aws=aws, resolve=resolve)
        result = json.loads((e2e / 'live-canary-evidence.json').read_text())
        assert result['status'] == status and result['candidateLock']['digest'] == A
        assert result['deploymentIdentity'] == before and result['runId'] == '202'
    else:
        with pytest.raises(binding.FetchError): binding.bind(tmp_path, BASE, config, aws=aws, resolve=resolve)
        assert 'candidateLock' not in json.loads((e2e / 'live-canary-evidence.json').read_text())
