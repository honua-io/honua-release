#!/usr/bin/env python3
"""Validate the actual mocked Terraform task command using the pinned proxy."""
import argparse
import json
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('terraform_test_jsonl')
args = parser.parse_args()
containers = None
with open(args.terraform_test_jsonl) as source:
    for line in source:
        event = json.loads(line)
        if event.get('type') != 'test_state':
            continue
        for row in event['test_state']['root_module'].get('resources', []):
            if row['address'] == 'aws_ecs_task_definition.console':
                containers = json.loads(row['values']['container_definitions'])
assert containers, 'mocked deployed task definition missing'
edge = next(row for row in containers if row['name'] == 'edge')
env = {row['name']: row['value'] for row in edge['environment']}
config = json.loads(env['HONUA_EDGE_CONFIG'])
# No network or credentials: substitute only discovery endpoints. Everything
# else (schema, injected headers, command, cookie flags, PKCE) stays as deployed.
provider = config['providers'][0]
provider['oidcConfig']['skipDiscovery'] = True
provider['oidcConfig']['jwksURL'] = 'https://issuer.invalid/keys'
provider['loginURL'] = 'https://issuer.invalid/authorize'
provider['redeemURL'] = 'https://issuer.invalid/token'
env['HONUA_EDGE_CONFIG'] = json.dumps(config)
env.update(OAUTH2_PROXY_CLIENT_SECRET='offline-client-secret', HONUA_EDGE_SHARED_SECRET='offline-edge-secret', OAUTH2_PROXY_COOKIE_SECRET='abcdefghijklmnopqrstuvwxyz012345')
command = ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--tmpfs', '/tmp:rw,noexec,nosuid,size=1m', '--entrypoint', edge['entryPoint'][0]]
for key, value in env.items():
    command += ['--env', key + '=' + value]
command += [edge['image'], *edge['entryPoint'][1:], edge['command'][0] + ' --config-test']
subprocess.run(command, check=True)
print('Actual Terraform-generated edge command/config accepted by pinned proxy without network or credentials.')
