"""Private, identity-only handoff from an authenticated saved-plan apply.

This records provisioning references, never human login or approval. The native
consumer must independently authenticate the successful original workflow job.
"""
from __future__ import annotations

import hashlib
from collections import Counter
import json
from pathlib import Path
import re

from human_enrollment import validate_configuration

SCHEMA = 'honua.hosted-pilot-human-identities/v1'
SHA = re.compile(r'[a-f0-9]{64}')
VERSION = re.compile(r'[A-Za-z0-9._+/=-]{1,1024}')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def positive_integer(value):
    require(isinstance(value, str) and re.fullmatch(r'[1-9][0-9]{0,19}', value),
            'original positive workflow identifier required')
    return int(value)


def version(value):
    require(isinstance(value, str) and VERSION.fullmatch(value) and value != 'null',
            'immutable private object version required')
    return value


def private_bytes(path, data):
    with Path(path).open('wb') as stream:
        stream.write(data)
    Path(path).chmod(0o600)


def original_review(review, raw, cfg_raw, cfg_path, env, root_path):
    """Refuse extra fields before copying the original public review privately."""
    require(json.loads(raw) == review and set(review) == {'schema', 'context', 'operation', 'config_path',
        'config_sha256', 'backend_sha256', 'root_path', 'lock_sha256', 'plan_sha256',
        'budget', 'created_at', 'resource_changes', 'action_counts', 's3_version_id'},
        'original saved-plan review contains unknown or missing fields')
    require(review['schema'] == 'honua.hosted-pilot-saved-plan/v1' and
            review['operation'] == env['PILOT_OPERATION'] == 'apply' and
            review['root_path'] == root_path and review['config_path'] == cfg_path and
            review['config_sha256'] == hashlib.sha256(cfg_raw).hexdigest() and
            sha256(raw) == 'sha256:' + env['PILOT_REVIEW_SHA256'],
            'original reviewed configuration or review digest differs')
    require(env['GITHUB_REPOSITORY'] == 'honua-io/honua-release' and
            env['GITHUB_REF'] == 'refs/heads/trunk' and
            env['GITHUB_EVENT_NAME'] == 'workflow_dispatch' and
            re.fullmatch(r'[a-f0-9]{40}', env['GITHUB_SHA']),
            'enrollment requires the reviewed trunk deployment workflow')
    require(review['context'] == {name: env[name] for name in
            ('GITHUB_REPOSITORY', 'GITHUB_SHA', 'GITHUB_RUN_ID')} |
            {'GITHUB_RUN_ATTEMPT': env['PILOT_PLAN_ATTEMPT']},
            'enrollment saved-plan producer differs')
    for field in ('config_sha256', 'backend_sha256', 'lock_sha256', 'plan_sha256'):
        require(isinstance(review[field], str) and SHA.fullmatch(review[field]),
                'original saved-plan digest required')
    require(review['budget'] == {'version_id': env['PILOT_BUDGET_VERSION_ID'],
            'sha256': env['PILOT_BUDGET_SHA256']}, 'original budget binding differs')
    version(review['s3_version_id'])
    for row in review['resource_changes']:
        require(isinstance(row, dict) and set(row) == {'address', 'actions'} and
                isinstance(row['address'], str) and
                re.fullmatch(r'[A-Za-z0-9_.\[\]"\-]+', row['address']) and
                isinstance(row['actions'], list) and all(action in
                ('no-op', 'create', 'read', 'update', 'delete', 'forget') for action in row['actions']),
                'saved-plan review is not value-free')
    require(review['action_counts'] == dict(Counter(action for row in review['resource_changes'] for action in row['actions'])),
            'saved-plan action counts differ')


def project(*, configuration, outputs, review, cfg_path, env, root_path):
    campaign = root_path == 'certification/hosted-pilot/campaign'
    app = configuration['application'] if campaign else configuration
    enrollment = app.get('human_enrollment')
    if enrollment is None:
        return None
    require(root_path in ('certification/hosted-pilot', 'certification/hosted-pilot/campaign'),
            'human enrollment requires the reviewed application root')
    region, account = env['PILOT_REGION'], env['PILOT_ACCOUNT_ID']
    require(re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', region) and
            re.fullmatch(r'[0-9]{12}', account), 'reviewed AWS region/account required')
    validate_configuration(enrollment, region=region, custody_key_arn=env['PILOT_STATE_KMS_ARN'])
    if campaign:
        observed = outputs['application_deployment']['value']
    else:
        observed = {key: outputs[key]['value'] for key in
                    ('deployment', 'identity_preparation', 'human_enrollment')}
    deployment, identity, humans = (observed[key] for key in
                                   ('deployment', 'identity_preparation', 'human_enrollment'))
    candidate = app['candidate']
    require(deployment['claimed_candidate'] == candidate and
            SHA.fullmatch(candidate['manifest_sha256']), 'applied candidate differs from reviewed candidate')
    candidate_key = 'sha256:' + candidate['manifest_sha256']
    if campaign:
        require(configuration['candidate_digest'] == candidate_key and
                configuration['server_image'] == candidate['server_image'],
                'campaign and application candidate differ')
    prefix = configuration['name_prefix'].removesuffix('-agent') if campaign else configuration['name_prefix']
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]{4,16}', prefix), 'reviewed pilot prefix required')
    pool = enrollment['pool_id']
    issuer = f'https://cognito-idp.{region}.amazonaws.com/{pool}'
    require(identity['pool_id'] == pool and identity['issuer'] == issuer and
            identity['groups'] == {'authors': prefix + '-authors', 'approvers': prefix + '-approvers'},
            'applied Cognito pool, issuer or roles differ')
    require(isinstance(humans, dict) and set(humans) == set(enrollment['users']),
            'applied subjects differ from the reviewed nominations')
    users = {}
    for alias, nominated in enrollment['users'].items():
        person = humans[alias]
        require(isinstance(person, dict) and set(person) == {'subject_id', 'role', 'status'} and
                person['role'] == nominated['role'] and
                re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', person['subject_id']) and
                person['status'] in ('FORCE_CHANGE_PASSWORD', 'CONFIRMED'),
                'applied human subject, role or status is not admissible')
        users[alias] = {'subjectId': person['subject_id'], 'role': person['role'], 'status': person['status']}
    require(len({person['subjectId'] for person in users.values()}) == len(users),
            'distinct human subjects are required')
    cluster = deployment['ecs_cluster']
    require(isinstance(cluster, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,255}', cluster),
            'actual ECS cluster name required')
    arn = f'arn:aws:ecs:{region}:{account}:'
    result = {'clusterArn': arn + 'cluster/' + cluster}
    for kind in ('server', 'console'):
        service, task = deployment[kind + '_service'], deployment[kind + '_task_definition']
        require(isinstance(service, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,255}', service) and
                isinstance(task, str) and re.fullmatch(re.escape(arn) + r'task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*', task),
                'actual applied service and task revision required')
        image = candidate[kind + '_image']
        require(isinstance(image, str) and re.fullmatch(r'[A-Za-z0-9.:/_-]+@sha256:[a-f0-9]{64}', image),
                'immutable applied candidate image required')
        result[kind + 'ServiceArn'] = arn + 'service/' + cluster + '/' + service
        result[kind + 'TaskDefinitionArn'] = task
        result[kind + 'Image'] = image
    origin = deployment['server_origin']
    expected_domain = configuration['target_domain'] if campaign else configuration['server_domain']
    require(origin == 'https://' + expected_domain, 'applied Server origin differs from reviewed target')
    result['serverOrigin'] = origin
    return {
        'schema': SCHEMA, 'accountId': account, 'candidateKey': candidate_key,
        'producer': {'repository': env['GITHUB_REPOSITORY'], 'workflow': '.github/workflows/hosted-pilot-deploy.yml',
            'sourceSha': env['GITHUB_SHA'], 'runId': positive_integer(env['GITHUB_RUN_ID']),
            'runAttempt': positive_integer(env['GITHUB_RUN_ATTEMPT']), 'jobKey': 'apply',
            'ref': env['GITHUB_REF'], 'event': env['GITHUB_EVENT_NAME']},
        'sourceConfiguration': {'path': cfg_path, 'sha256': 'sha256:' + review['config_sha256']},
        'plan': {'sha256': 'sha256:' + review['plan_sha256'], 'versionId': review['s3_version_id'],
            'attempt': positive_integer(env['PILOT_PLAN_ATTEMPT']),
            'reviewSha256': 'sha256:' + env['PILOT_REVIEW_SHA256'],
            'reviewArtifactId': positive_integer(env['PILOT_REVIEW_ARTIFACT_ID'])},
        'identity': {'region': region, 'poolId': pool, 'issuer': issuer,
            'authorRole': prefix + '-authors', 'approverRole': prefix + '-approvers'},
        'deployment': result, 'users': users, 'qualification': False,
    }


def retain_once(path, *, key, env, aws):
    """One conditional PUT; an ambiguous result permits only exact GET recovery."""
    data = Path(path).read_bytes()
    bucket, kms = env['PILOT_PLAN_BUCKET'], env['PILOT_PLAN_KMS_ARN']
    try:
        written = aws('s3api', 'put-object', '--bucket', bucket, '--key', key, '--body', str(path),
                      '--if-none-match', '*', '--server-side-encryption', 'aws:kms', '--ssekms-key-id', kms)
    except (ValueError, OSError):
        written = {}
    # Read after every write, including a lost reply; never replay the mutation.
    readback = Path(path).with_suffix('.readback')
    args = ['s3api', 'get-object', '--bucket', bucket, '--key', key]
    if written.get('VersionId') not in (None, 'null'):
        args += ['--version-id', version(written['VersionId'])]
    observed = aws(*args, str(readback))
    observed_version = version(observed.get('VersionId'))
    require(observed.get('ServerSideEncryption') == 'aws:kms' and observed.get('SSEKMSKeyId') == kms and
            (not written.get('VersionId') or written['VersionId'] == observed_version) and
            readback.stat().st_size == len(data) and readback.read_bytes() == data,
            'private enrollment object readback differs; retain original effects')
    readback.chmod(0o600)
    return {'bucket': bucket, 'key': key, 'versionId': observed_version, 'sha256': sha256(data), 'kmsArn': kms}


def emit(*, cfg, root, root_path, outputs, review, work, env, aws):
    cfg_raw = Path(cfg).read_bytes()
    configuration = json.loads(cfg_raw)
    app = configuration.get('application', {}) if root_path.endswith('/campaign') else configuration
    if app.get('human_enrollment') is None:
        return None
    cfg_path = str(Path(cfg).relative_to(root))
    raw = (work / 'review.json').read_bytes()
    original_review(review, raw, cfg_raw, cfg_path, env, root_path)
    carrier = project(configuration=configuration, outputs=outputs, review=review,
                      cfg_path=cfg_path, env=env, root_path=root_path)
    prefix = f"hosted-pilot/plans/{carrier['producer']['runId']}/{carrier['plan']['attempt']}/"
    carrier['plan']['review'] = retain_once(work / 'review.json', key=prefix + 'review.json', env=env, aws=aws)
    path = work / 'human-identities.json'
    private_bytes(path, (json.dumps(carrier, sort_keys=True, indent=2) + '\n').encode())
    reference = retain_once(path, key=prefix + f"human-identities-{carrier['producer']['runAttempt']}.json", env=env, aws=aws)
    # This reference alone may enter the public workflow artifact. No users do.
    private_bytes(work / 'human-enrollment-reference.json',
                  (json.dumps(reference, sort_keys=True, indent=2) + '\n').encode())
    return reference
