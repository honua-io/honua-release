#!/usr/bin/env python3
"""Read-only infrastructure/anonymous-refusal checks; never a human qualification."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, build_opener, HTTPRedirectHandler
from deploy import TF, ROOT, aws, require, private_json, run
from image_manifest import verify as verify_image


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get(url, headers=None):
    try:
        response = build_opener(NoRedirect).open(Request(url, headers=headers or {}), timeout=20)
    except HTTPError as error:
        response = error
    with response:
        return response.status, dict(response.headers)


def refuse_console(origin, hosted_login, *, forged=False):
    headers = {'X-Forwarded-User': 'forged-approver', 'X-Forwarded-Email': 'forged@example.invalid', 'X-Forwarded-Access-Token': 'forged', 'X-Honua-Edge-Auth': 'forged', 'Authorization': 'Bearer forged', 'X-API-Key': 'forged'} if forged else {}
    target = origin + '/admin'
    for _ in range(4):
        status, response = get(target, headers)
        require(status in (302, 303, 307), 'anonymous/forged Console request did not redirect to authentication')
        location = response.get('Location', response.get('location', ''))
        if location.startswith('/'):
            location = origin + location
        if location.startswith(hosted_login + '/'):
            query = parse_qs(urlsplit(location).query)
            require(query.get('response_type') == ['code'] and query.get('code_challenge_method') == ['S256'] and query.get('state') and query.get('nonce'), 'edge login lacks code/PKCE/state/nonce binding')
            return
        require(location.startswith(origin + '/'), 'unexpected authentication redirect origin')
        target = location
    raise ValueError('edge login did not reach dedicated Cognito domain')


def observed_service(cluster, service, expected):
    status = aws('ecs', 'describe-services', '--cluster', cluster, '--services', service)
    require(not status.get('failures') and len(status['services']) == 1, 'missing deployed ECS service')
    row = status['services'][0]
    require(row['runningCount'] == row['desiredCount'] == 1 and row['pendingCount'] == 0, 'ECS deployment is not stable')
    require(len(row['deployments']) == 1 and row['deployments'][0]['rolloutState'] == 'COMPLETED', 'ECS deployment has not completed')
    tasks = aws('ecs', 'list-tasks', '--cluster', cluster, '--service-name', service)['taskArns']
    require(len(tasks) == 1, 'expected one isolated task')
    observed = aws('ecs', 'describe-tasks', '--cluster', cluster, '--tasks', *tasks)
    require(not observed.get('failures'), 'cannot inspect installed image identity')
    require(len(observed.get('tasks', [])) == 1, 'missing installed ECS task')
    task = observed['tasks'][0]
    definition = aws('ecs', 'describe-task-definition', '--task-definition', task['taskDefinitionArn'])['taskDefinition']
    require(definition['taskDefinitionArn'] == task['taskDefinitionArn'], 'installed task definition identity differs')
    platform = definition.get('runtimePlatform', {})
    containers = {item['name']: item for item in task['containers']}
    require(set(containers) == set(expected), 'installed task has unexpected or missing containers')
    installed = []
    for name, image in expected.items():
        require(name in containers and containers[name].get('image') == image, 'installed immutable image reference mismatch')
        installed.append(verify_image(image, containers[name].get('imageDigest'), architecture=platform.get('cpuArchitecture', 'X86_64'), operating_system=platform.get('operatingSystemFamily', 'LINUX'), aws=aws) | {'container': name, 'taskArn': task['taskArn']})
    return installed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--infrastructure-only", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    work = Path(os.environ['RUNNER_TEMP']) / 'hosted-pilot-private'
    outputs = json.loads((work / 'outputs.json').read_text())
    refs = outputs['deployment']['value']
    identity = outputs['identity_preparation']['value']
    cfg = json.loads((ROOT / os.environ['PILOT_CONFIG_PATH']).read_text())
    cluster = refs['ecs_cluster']
    for service in [refs['console_service'], refs['server_service']]:
        run(['aws', 'ecs', 'wait', 'services-stable', '--cluster', cluster, '--services', service])
    edge_image = cfg['edge_proxy_image'] if 'edge_proxy_image' in cfg else refs['edge_proxy_image']
    installed = observed_service(cluster, refs['console_service'], {'console': cfg['candidate']['console_image'], 'edge': edge_image})
    installed += observed_service(cluster, refs['server_service'], {'honua': cfg['candidate']['server_image']})
    if not args.infrastructure_only:
        origin = cfg['identity_redirects']['console_origin']
        refuse_console(origin, identity['hosted_login_url'])
        refuse_console(origin, identity['hosted_login_url'], forged=True)
        status, _ = get(refs['server_origin'] + '/api/v1/admin/auth/session', {'Authorization': 'Bearer forged', 'X-Forwarded-User': 'forged-approver'})
        require(status in (401, 403), 'Server accepted forged operator authentication')
    result = {'schema': 'honua.hosted-pilot-infrastructure-readiness/v1', 'source_sha': os.environ['GITHUB_SHA'], 'run_id': os.environ['GITHUB_RUN_ID'], 'installed_digests_match': True, 'installed_images': installed, 'anonymous_and_forged_edge_identity_refused': 'not-run' if args.infrastructure_only else True, 'forged_server_auth_refused': 'not-run' if args.infrastructure_only else True, 'human_auth_qualification': 'not-run', 'author_approver_separation': 'not-run', 'agent_and_model_campaign': 'not-run', 'status': 'ecs-ready-network-and-human-qualification-required' if args.infrastructure_only else 'infrastructure-ready-human-qualification-required'}
    private_json(work / 'readiness.json', result)
    print('Installed ECS digests and health checks pass. Inspect readiness.json for network and human checks still required.')


if __name__ == '__main__':
    main()
