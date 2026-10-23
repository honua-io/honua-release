#!/usr/bin/env python3
"""Build only a reviewed source archive; provision no registry or AWS resource."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from deploy import ROOT, require
from controller_archive import contract


def call(args, *, cwd=ROOT, capture=False, input=None):
    result = subprocess.run(args, cwd=cwd, input=input, capture_output=capture)
    require(result.returncode == 0, 'trusted controller image command failed: ' + args[0])
    return result.stdout.decode().strip() if capture else ''


def main():
    e = os.environ
    require(e['GITHUB_REPOSITORY'] == 'honua-io/honua-release' and e['GITHUB_REF'] == 'refs/heads/trunk', 'controller images require release trunk')
    source = e['GITHUB_SHA']
    require(re.fullmatch('[a-f0-9]{40}', source), 'immutable controller source required')
    require(call(['git', 'rev-parse', 'HEAD'], capture=True) == source, 'controller checkout differs from workflow source')
    require(re.fullmatch(r'\d{12}', e['PILOT_ACCOUNT_ID']) and re.fullmatch(r'[a-z]{2}-[a-z]+-\d', e['PILOT_REGION']), 'explicit controller account/region required')
    repository = e['PILOT_CONTROLLER_REPOSITORY']
    prefix = e['PILOT_ACCOUNT_ID'] + '.dkr.ecr.' + e['PILOT_REGION'] + '.amazonaws.com/'
    require(repository.startswith(prefix) and re.fullmatch('honua-pilot-[a-z0-9-]+-trusted-controller', repository[len(prefix):]), 'controller repository is outside reviewed account/region/namespace')
    run_id, attempt = e['GITHUB_RUN_ID'], e['GITHUB_RUN_ATTEMPT']
    require(run_id.isdecimal() and attempt.isdecimal() and int(run_id) > 0 and int(attempt) > 0, 'positive producing run identity required')
    account = json.loads(call(['aws', 'sts', 'get-caller-identity', '--output', 'json'], capture=True))
    require(account['Account'] == e['PILOT_ACCOUNT_ID'], 'controller image account mismatch')
    tag = source + '-' + run_id + '-' + attempt
    image = repository + ':' + tag
    source_tree = call(['git', 'rev-parse', source + '^{tree}'], capture=True)
    with tempfile.TemporaryDirectory(prefix='pilot-controller-', dir=e['RUNNER_TEMP']) as tmp:
        tmp = Path(tmp)
        archive, context = tmp / 'source.tar', tmp / 'context'
        context.mkdir()
        call(['git', 'archive', '--format=tar', '--output', str(archive), source])
        archive_digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        call(['tar', '-xf', str(archive), '-C', str(context)])
        runtime_contract = contract(context)
        call(['docker', 'build', '--platform', 'linux/amd64', '--build-arg', 'RELEASE_SOURCE_SHA=' + source,
              '--build-arg', 'RELEASE_TREE_ID=' + source_tree, '--build-arg', 'RELEASE_ARCHIVE_SHA256=' + archive_digest,
              '-f', str(context / 'certification/hosted-pilot/controller/Dockerfile'), '-t', image, str(context)])
        call(['docker', 'run', '--rm', '--read-only', '--network', 'none', '--entrypoint', 'python', image,
              'tools/hosted_pilot/controller_source.py', 'verify', '--source-sha', source, '--tree-id', source_tree,
              '--archive-sha256', archive_digest, '--receipt', '/opt/honua-controller-source.json'])
        call(['docker', 'run', '--rm', '--read-only', '--network', 'none', '--entrypoint', 'python', image, '-c',
              'import sys; sys.path.insert(0,"tools"); import boto3, psycopg, yaml, jsonschema, cryptography, agent_e2e_campaign, agent_e2e_fixture_database'])
        call(['docker', 'run', '--rm', '--read-only', '--network', 'none', '--entrypoint', 'python', image, '-c',
              'import os,pathlib,tempfile; assert os.geteuid()==10001; p=pathlib.Path(tempfile.mkstemp(dir="/work")[1]); p.write_text("private"); p.unlink(); assert not os.access("/controller",os.W_OK)'])
        # Only the registry login token enters docker's stdin, never logs/artifacts.
        password = subprocess.run(['aws', 'ecr', 'get-login-password', '--region', e['PILOT_REGION']], capture_output=True)
        require(password.returncode == 0, 'controller registry login refused')
        call(['docker', 'login', '--username', 'AWS', '--password-stdin', repository.split('/')[0]], input=password.stdout)
        try:
            call(['docker', 'push', image])
        finally:
            call(['docker', 'logout', repository.split('/')[0]])
    digest = call(['aws', 'ecr', 'describe-images', '--region', e['PILOT_REGION'], '--repository-name', repository[len(prefix):], '--image-ids', 'imageTag=' + tag, '--query', 'imageDetails[0].imageDigest', '--output', 'text'], capture=True)
    require(re.fullmatch('sha256:[a-f0-9]{64}', digest), 'immutable controller registry digest missing')
    receipt = {'schema': 'honua.hosted-controller-image/v1', 'repository': e['GITHUB_REPOSITORY'], 'source_sha': source, 'tree_id': source_tree, 'archive_sha256': archive_digest, 'workflow': 'hosted-pilot-controller-image.yml', 'run_id': run_id, 'run_attempt': attempt, 'image': repository + '@' + digest, 'image_digest': digest, 'platform': 'linux/amd64', 'runtime_contract_sha256': runtime_contract}
    (Path(e['RUNNER_TEMP']) / 'hosted-pilot-controller-image.json').write_text(json.dumps(receipt, sort_keys=True) + '\n')
    print('Verified trusted controller image published with immutable source receipt.')


if __name__ == '__main__':
    main()
