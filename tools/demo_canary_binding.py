#!/usr/bin/env python3
"""Read-only selected-lock preparation and ECS demo deployment binding.

This produces burn evidence only from an authenticated retained mint, never the
checkout's lock or an operator-supplied JSON lock. It does not deploy anything.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
from urllib.parse import urlsplit

import yaml

from fetch_promotion_evidence import (CANDIDATE_ARTIFACT, GitHub, MINTING_EVENTS,
                                      MINTING_WORKFLOWS, FetchError, _identity, _retain, _run_id)

REPOSITORY = 'honua-io/honua-release'
RECORD = re.compile(r'certification/promotions/([0-9]+\.[0-9]+(?:\.[0-9]+)?-rc\.[1-9][0-9]*)\.json')


def require(ok, reason):
    if not ok:
        raise FetchError(reason)


def checked(args):
    result = subprocess.run(args, text=True, capture_output=True)
    require(result.returncode == 0, 'read/verification command failed: ' + ' '.join(args[:3]))
    return result.stdout


def read(path):
    return json.loads(path.read_bytes())


def prepare(record_path, root, *, gh=None, run=checked, ref=None):
    gh = gh or GitHub(REPOSITORY)
    match = RECORD.fullmatch(str(record_path))
    require(match is not None and not record_path.is_symlink(), 'selected promotion record must be a canonical committed path')
    repo = gh.api()
    require(repo.get('full_name') == REPOSITORY and repo.get('default_branch') == 'trunk' and ref == 'refs/heads/trunk',
            'bound canary must execute on the release repository default branch')
    require(gh.api('branches/trunk').get('protected') is True, 'bound canary requires protected trunk')
    # Checkout is pinned to the workflow SHA; a later trunk move must not supply the record.
    tracked = run(['git', 'ls-files', '--error-unmatch', '--', str(record_path)]).strip()
    require(tracked == str(record_path), 'selected promotion record is not tracked')
    record = read(record_path)
    label = match[1]
    require(record.get('platformLabel') == label, 'selected record label/path differ')
    run_id = _run_id(record.get('rcTrainRunId'), 'rcTrainRunId')
    train = gh.api('actions/runs/' + run_id)
    _identity(train, REPOSITORY, 'trunk', run_id, MINTING_WORKFLOWS, MINTING_EVENTS)
    require(re.fullmatch(r'[0-9a-f]{40}', train.get('head_sha', '')) is not None, 'mint source SHA missing')
    require(_retain(gh, run_id, CANDIDATE_ARTIFACT, root, train), 'retained mint artifact unavailable')
    lock_path = root / 'platform-lock.json'
    lock = read(lock_path)
    digest = 'sha256:' + hashlib.sha256(lock_path.read_bytes()).hexdigest()
    require(record.get('lock', {}).get('digest') == digest and lock.get('platform', {}).get('id') == 'honua-' + label,
            'selected record does not bind the retained lock bytes/label')
    bundle = str(root / 'platform-lock.sigstore.json')
    if train['path'] == '.github/workflows/release-train.yml':
        run(['gh', 'attestation', 'verify', str(lock_path), '--bundle', bundle, '--repo', REPOSITORY,
             '--signer-workflow', REPOSITORY + '/.github/workflows/release-train.yml',
             '--source-digest', train['head_sha'], '--source-ref', 'refs/heads/trunk', '--deny-self-hosted-runners'])
    else:
        run(['cosign', 'verify-blob', str(lock_path), '--bundle', bundle,
             '--certificate-identity', 'https://github.com/' + REPOSITORY + '/.github/workflows/nightly-certification.yml@refs/heads/trunk',
             '--certificate-oidc-issuer', 'https://token.actions.githubusercontent.com',
             '--certificate-github-workflow-repository', REPOSITORY,
             '--certificate-github-workflow-sha', train['head_sha'], '--certificate-github-workflow-ref', 'refs/heads/trunk'])
    run(['python', 'tools/platform_lock_bundle.py', str(lock_path), '--manifest', str(root / 'platform-manifest.yaml'),
         '--matrix', str(root / 'compatibility-matrix.yaml'), '--label', label, '--out-dir', str(root), '--check'])
    server = lock['components']['honua-server']['source']['revision']
    require(re.fullmatch(r'[0-9a-f]{40}', server) is not None, 'selected server revision missing')
    return {'record': str(record_path), 'digest': digest, 'runId': run_id, 'serverSha': server}


def image_repository(value):
    value = value.split('@')[0]
    return value.rsplit(':', 1)[0] if ':' in value.rsplit('/', 1)[-1] else value


def observe(root, base, config, *, aws=None, resolve=None):
    """Resolve endpoint -> ALB -> target group -> stable ECS tasks -> selected image.

    The initial adapter intentionally requires a direct Route53 alias, one HTTPS
    listener, IP targets and a stable ECS service. Unsupported topology refuses.
    """
    required = ('region', 'cluster', 'service', 'container', 'zone')
    require(all(isinstance(config.get(k), str) and config[k].strip() for k in required),
            'bound ECS canary requires approved region/cluster/service/container/Route53 zone inputs')
    url = urlsplit(base)
    require(url.scheme == 'https' and bool(url.hostname) and not url.username and not url.password
            and url.port in (None, 443) and url.path in ('', '/') and not url.query and not url.fragment,
            'bound canary requires an HTTPS root endpoint')
    if aws is None:
        def aws(service, command, *args):
            return json.loads(checked(['aws', service, command, *args, '--region', config['region'], '--output', 'json']))
    response = aws('ecs', 'describe-services', '--cluster', config['cluster'], '--services', config['service'])
    require(not response.get('failures') and len(response.get('services', [])) == 1, 'ECS service unavailable')
    service = response['services'][0]
    require(bool(service.get('serviceArn')) and bool(service.get('taskDefinition')), 'ECS service identity missing')
    count = service.get('desiredCount', 0)
    deployments = service.get('deployments', [])
    require(service.get('status') == 'ACTIVE' and count > 0 and service.get('runningCount') == count
            and service.get('pendingCount') == 0 and len(deployments) == 1
            and deployments[0].get('status') == 'PRIMARY' and deployments[0].get('rolloutState') == 'COMPLETED',
            'ECS deployment is not stable')
    lbs = service.get('loadBalancers', [])
    require(len(lbs) == 1 and lbs[0].get('containerName') == config['container'], 'ECS server load-balancer binding missing')
    tg = lbs[0]['targetGroupArn']
    port = lbs[0]['containerPort']
    groups = aws('elbv2', 'describe-target-groups', '--target-group-arns', tg).get('TargetGroups', [])
    require(len(groups) == 1 and groups[0].get('TargetType') == 'ip' and len(groups[0].get('LoadBalancerArns', [])) == 1,
            'unsupported target group topology')
    lb = groups[0]['LoadBalancerArns'][0]
    balances = aws('elbv2', 'describe-load-balancers', '--load-balancer-arns', lb).get('LoadBalancers', [])
    require(len(balances) == 1 and balances[0].get('State', {}).get('Code') == 'active'
            and balances[0].get('Scheme') == 'internet-facing', 'public ALB unavailable')
    alias = balances[0]['DNSName'].rstrip('.').removeprefix('dualstack.')
    records = aws('route53', 'list-resource-record-sets', '--hosted-zone-id', config['zone']).get('ResourceRecordSets', [])
    routes = [r for r in records if r.get('Name', '').rstrip('.').lower() == url.hostname.lower() and r.get('Type') in ('A', 'AAAA')]
    require(any(r.get('Type') == 'A' for r in routes) and all(
            not any(k in r for k in ('SetIdentifier', 'Weight', 'Failover', 'Region'))
            and r.get('AliasTarget', {}).get('DNSName', '').rstrip('.').removeprefix('dualstack.') == alias
            and r.get('AliasTarget', {}).get('HostedZoneId') == balances[0].get('CanonicalHostedZoneId') for r in routes),
            'endpoint Route53 alias does not bind the ECS ALB')
    resolve = resolve or (lambda host: {item[4][0] for item in socket.getaddrinfo(host, 443, socket.AF_UNSPEC, socket.SOCK_STREAM)})
    addresses = resolve(url.hostname)
    require(bool(addresses) and addresses == resolve(routes[0]['AliasTarget']['DNSName'].rstrip('.')), 'live endpoint DNS differs from the observed ALB')
    listeners = aws('elbv2', 'describe-listeners', '--load-balancer-arn', lb).get('Listeners', [])
    listeners = [l for l in listeners if l.get('Port') == 443 and l.get('Protocol') == 'HTTPS']
    require(len(listeners) == 1, 'endpoint HTTPS listener missing')
    rules = aws('elbv2', 'describe-rules', '--listener-arn', listeners[0]['ListenerArn']).get('Rules', [])
    require(bool(rules), 'HTTPS routing rules missing')
    for rule in rules:
        actions = rule.get('Actions', [])
        require(len(actions) == 1 and actions[0].get('Type') == 'forward'
                and actions[0].get('TargetGroupArn') == tg
                and all(t.get('TargetGroupArn') == tg for t in actions[0].get('ForwardConfig', {}).get('TargetGroups', [])),
                'HTTPS listener can route outside the selected ECS target group')
    arns = aws('ecs', 'list-tasks', '--cluster', config['cluster'], '--service-name', config['service'],
               '--desired-status', 'RUNNING').get('taskArns', [])
    require(len(arns) == count and len(set(arns)) == count, 'ECS running task set differs from desired count')
    described = aws('ecs', 'describe-tasks', '--cluster', config['cluster'], '--tasks', *arns)
    tasks = described.get('tasks', [])
    require(not described.get('failures') and {t.get('taskArn') for t in tasks} == set(arns), 'ECS task observations incomplete')
    manifest = yaml.safe_load((root / 'platform-manifest.yaml').read_bytes())
    server = manifest['components']['honua-server']
    arch = {'x86_64': 'amd64', 'arm64': 'arm64'}.get(server.get('awsEcsArchitecture', 'x86_64'))
    require(arch is not None, 'unsupported ECS architecture')
    accepted = {server['digest'], server.get('platformDigests', {}).get(arch)} - {None, ''}
    repository = image_repository(server['image'])
    ips = set()
    for task in tasks:
        require(task.get('lastStatus') == 'RUNNING' and task.get('taskDefinitionArn') == service.get('taskDefinition'),
                'task is not on the stable service revision')
        containers = [c for c in task.get('containers', []) if c.get('name') == config['container']]
        require(len(containers) == 1 and containers[0].get('lastStatus') == 'RUNNING'
                and image_repository(containers[0].get('image', '')) == repository
                and containers[0].get('imageDigest') in accepted, 'running ECS image differs from selected lock')
        addresses = [d['value'] for a in task.get('attachments', []) if a.get('type') == 'ElasticNetworkInterface'
                     for d in a.get('details', []) if d.get('name') == 'privateIPv4Address']
        require(len(addresses) == 1, 'task network identity missing')
        ips.add((addresses[0], port))
    targets = aws('elbv2', 'describe-target-health', '--target-group-arn', tg).get('TargetHealthDescriptions', [])
    require(len(targets) == count and {(t.get('Target', {}).get('Id'), t.get('Target', {}).get('Port')) for t in targets} == ips
            and all(t.get('TargetHealth', {}).get('State') == 'healthy' for t in targets), 'ALB targets differ from healthy selected tasks')
    return {'target': 'aws-ecs-demo', 'endpoint': base, 'serviceArn': service.get('serviceArn'),
            'taskDefinitionArn': service.get('taskDefinition'), 'taskArns': sorted(arns),
            'targetGroupArn': tg, 'loadBalancerArn': lb, 'imageDigest': server['digest']}


def bind(root, base, config, **observations):
    after = observe(root, base, config, **observations)
    require(after == read(root / 'deployment-before.json'), 'deployment changed during canary probes')
    binding = read(root / 'binding.json')
    envelope_path = Path('e2e/live-canary-evidence.json')
    envelope = read(envelope_path)
    require(envelope.get('candidateServerSha') == binding['serverSha'] and envelope.get('targetUrl') == base,
            'probed deployment revision/endpoint differs from selected candidate')
    envelope.update(runId=os.environ['GITHUB_RUN_ID'], status=read(Path('e2e/gate-report-demo-canary.json'))['status'],
                    candidateLock={'path': binding['record'], 'digest': binding['digest']},
                    deploymentIdentity=after, mintingRunId=binding['runId'])
    envelope_path.write_text(json.dumps(envelope, indent=2) + '\n')
    # The complete burn ledger reads the workflow conclusion, so an incomplete
    # bound observation must also fail Actions after retaining its truthful receipt.
    require(envelope['status'] == 'pass', 'bound canary did not pass; selected-lock burn is interrupted')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('mode', choices=('prepare', 'observe', 'bind'))
    ap.add_argument('--record', type=Path)
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--base', required=True)
    args = ap.parse_args()
    config = {key: os.environ.get('DEMO_CANARY_' + key.upper(), '') for key in ('region', 'cluster', 'service', 'container', 'zone')}
    args.root.mkdir(parents=True, exist_ok=True)
    if args.mode == 'prepare':
        binding = prepare(args.record, args.root, ref=os.environ.get('GITHUB_REF'))
        (args.root / 'binding.json').write_text(json.dumps(binding))
        with open(os.environ['GITHUB_ENV'], 'a') as out:
            out.write('HONUA_CANARY_CANDIDATE_SHA=' + binding['serverSha'] + '\n')
    elif args.mode == 'observe':
        (args.root / 'deployment-before.json').write_text(json.dumps(observe(args.root, args.base, config)))
    else:
        bind(args.root, args.base, config)


if __name__ == '__main__':
    main()
