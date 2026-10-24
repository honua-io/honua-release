"""Fixed one-shot registrar entry; no infrastructure creation or runtime activation."""
from __future__ import annotations

import copy
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler

from agent_e2e_pack import canonical_digest
from agent_e2e_setup_http import require

CONFIG_ENV = 'PILOT_NATIVE_RUNTIME_REGISTRATION_CONFIG'
COMMAND = ['python', 'tools/hosted_pilot/native_runtime_registration.py']
SECRET_ENVS = {'registrar': 'PILOT_FIXTURE_ADMIN_PASSWORD',
               'control': 'HONUA_EXTERNAL_EXECUTION_CONTROL_KEY', 'task': 'HONUA_AGENT_REQUESTER_KEY'}


def validate(config):
    fields = {'schemaVersion', 'region', 'accountId', 'candidateKey', 'state', 'generationDigest',
              'bootstrapArtifactDigest', 'operation', 'declaration', 'clusterArn',
              'taskDefinitionFamily', 'imageUri', 'credentials', 'maximumUsd', 'maxExecutionSeconds', 'liabilityQuote'}
    require(isinstance(config, dict) and set(config) == fields and type(config['schemaVersion']) is int and
            config['schemaVersion'] == 1, 'exact registrar configuration is required')
    require(re.fullmatch(r'[a-z]{2}-[a-z]+-[0-9]', str(config['region'])) and
            re.fullmatch(r'[0-9]{12}', str(config['accountId'])) and
            all(re.fullmatch(r'sha256:[a-f0-9]{64}', str(config[name])) for name in
                ('candidateKey', 'generationDigest', 'bootstrapArtifactDigest')), 'registrar original identity differs')
    region, account = config['region'], config['accountId']
    name = 'honua-agent-'+config['candidateKey'][7:31]
    require(config['state'] == {'region': region, 'bucket': name+'-'+account, 'table': name+'-state'},
            'registrar cannot substitute the original candidate store')
    declaration = config['declaration']
    require(isinstance(declaration, dict) and set(declaration) == {'path', 'sha256'} and
            re.fullmatch(r'certification/agent-e2e/installed-runtimes/[A-Za-z0-9_-]{1,128}\.json', str(declaration['path'])) and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(declaration['sha256'])), 'registrar declaration source differs')
    runtime = Path(declaration['path']).stem
    family = re.fullmatch(r'arn:aws:ecs:'+re.escape(region)+':'+account+
                         r':task-definition/(honua-pilot-[a-z0-9-]+)-runtime-registration', str(config['taskDefinitionFamily']))
    require(family and config['clusterArn'] == 'arn:aws:ecs:'+region+':'+account+':cluster/'+family[1]+'-cluster' and
            re.fullmatch(account+r'\.dkr\.ecr\.'+re.escape(region)+r'\.amazonaws\.com/'+
                         re.escape(family[1].removesuffix('-agent'))+r'-trusted-controller@sha256:[a-f0-9]{64}', str(config['imageUri'])),
            'registrar must use its isolated original cluster and immutable trusted image')
    credentials = config['credentials']
    require(isinstance(credentials, dict) and set(credentials) == set(SECRET_ENVS), 'registrar needs exactly three credentials')
    for ref in credentials.values():
        require(isinstance(ref, dict) and set(ref) == {'secretArn', 'versionId'} and
                re.fullmatch(r'arn:aws:secretsmanager:'+re.escape(region)+':'+account+r':secret:[A-Za-z0-9/_+=.@-]+', str(ref['secretArn'])) and
                re.fullmatch(r'[A-Za-z0-9-]{32,64}', str(ref['versionId'])), 'registrar requires immutable private credential versions')
    require(len({ref['secretArn'] for ref in credentials.values()}) == 3, 'registrar credentials cannot share a secret')
    require(type(config['maxExecutionSeconds']) is int and 1 <= config['maxExecutionSeconds'] <= 600 and
            isinstance(config['maximumUsd'], dict) and set(config['maximumUsd']) == {'infrastructure', 'cleanup'} and
            all(isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?', value) and Decimal(value) > 0
                for value in config['maximumUsd'].values()), 'registrar requires exact positive resource bounds and a bounded original lifetime')
    quote = config['liabilityQuote']
    require(isinstance(quote, dict) and set(quote) == {'path', 'sha256'} and
            re.fullmatch(r'certification/hosted-pilot/quotes/[A-Za-z0-9_./-]+\.json', str(quote['path'])) and
            '..' not in quote['path'].split('/') and
            re.fullmatch(r'sha256:[a-f0-9]{64}', str(quote['sha256'])), 'registrar requires its checked-source liability quote')
    operation = config['operation']
    require(isinstance(operation, dict) and set(operation) == {'operationId', 'liabilityBasisDigest', 'reservationIds'} and
            operation['operationId'] == 'runtime-registration-'+runtime and
            operation['liabilityBasisDigest'] == canonical_digest({key: value for key, value in config.items() if key != 'operation'}) and
            isinstance(operation['reservationIds'], dict) and set(operation['reservationIds']) == {'infrastructure', 'cleanup'} and
            all(isinstance(value, str) and value for value in operation['reservationIds'].values()) and
            len(set(operation['reservationIds'].values())) == 2, 'registrar needs its own original effect and cleanup liabilities')
    return runtime


def authorize(config, durable, *, source_reader=None):
    from agent_e2e_runtime_configuration import source_file
    from registration_allowance import verify_retained_admission
    validate(config)
    try:
        admitted = verify_retained_admission(config, durable, source_reader=source_reader or source_file, clock=time.time)
    except ValueError:
        require(False, 'registrar original admission, quote or funding is missing, changed or expired')
    return admitted['originalFundingExpiresEpoch']


def verify_task(config, task_arn, *, ecs, image_verifier):
    observed = ecs.describe_tasks(cluster=config['clusterArn'], tasks=[task_arn])
    require(not observed.get('failures') and len(observed.get('tasks', [])) == 1, 'registrar current task cannot be observed')
    task = observed['tasks'][0]
    require(task.get('taskArn') == task_arn and task.get('clusterArn') == config['clusterArn'] and
            task.get('lastStatus') == 'RUNNING' and task.get('desiredStatus') == 'RUNNING' and
            task.get('launchType') == 'FARGATE' and task.get('enableExecuteCommand') is False and
            re.fullmatch(re.escape(config['taskDefinitionFamily'])+r':[1-9][0-9]*', str(task.get('taskDefinitionArn'))) and
            len(task.get('containers', [])) == 1, 'registrar current task identity or isolation differs')
    overrides = task.get('overrides', {})
    require(set(overrides) <= {'containerOverrides'} and all(set(row) <= {'name'} and
            row.get('name') == 'runtime-registration' for row in overrides.get('containerOverrides', [])),
            'registrar command, environment and role overrides are forbidden')
    definition = ecs.describe_task_definition(taskDefinition=task['taskDefinitionArn'])['taskDefinition']
    require(definition.get('taskDefinitionArn') == task['taskDefinitionArn'] and definition.get('status') == 'ACTIVE' and
            definition.get('networkMode') == 'awsvpc' and definition.get('runtimePlatform') ==
            {'cpuArchitecture': 'X86_64', 'operatingSystemFamily': 'LINUX'} and
            definition.get('cpu') == '256' and definition.get('memory') == '1024' and
            definition.get('ephemeralStorage', {'sizeInGiB': 20}) == {'sizeInGiB': 20} and
            len(definition.get('containerDefinitions', [])) == 1, 'registrar task definition differs')
    container = definition['containerDefinitions'][0]
    live = task['containers'][0]
    require(container.get('name') == live.get('name') == 'runtime-registration' and
            container.get('image') == live.get('image') == config['imageUri'] and container.get('command') == COMMAND and
            container.get('user') == '10001:10001' and container.get('readonlyRootFilesystem') is True and
            not container.get('privileged', False) and not container.get('entryPoint') and not container.get('portMappings') and
            not container.get('environmentFiles') and not container.get('volumesFrom') and
            container.get('mountPoints') == [{'sourceVolume': 'work', 'containerPath': '/work', 'readOnly': False}],
            'registrar container command or isolation differs')
    environment = container.get('environment', [])
    require(len(environment) == 1 and environment[0].get('name') == CONFIG_ENV and
            json.loads(environment[0]['value']) == config, 'registrar environment is not its immutable task configuration')
    expected = {SECRET_ENVS[kind]: ref['secretArn']+':::'+ref['versionId'] for kind, ref in config['credentials'].items()}
    secrets = container.get('secrets', [])
    require(len(secrets) == 3 and {row['name']: row['valueFrom'] for row in secrets} == expected,
            'registrar private injections do not match their exact original versions')
    image = image_verifier(config['imageUri'], live.get('imageDigest'))
    def native_json(value):
        if isinstance(value, datetime) and value.tzinfo is not None:
            return value.isoformat()
        raise TypeError('unsupported native task definition value')
    return {'taskArn': task_arn, 'taskDefinitionArn': task['taskDefinitionArn'],
            'definitionDigest': canonical_digest(json.loads(json.dumps(definition, default=native_json))), 'taskRoleArn': definition['taskRoleArn'],
            'executionRoleArn': definition['executionRoleArn'], 'image': image}


def bind_original_credentials(config, verified, durable, native, *, ecs, source_reader=None):
    require(verified['candidateKey'] == config['candidateKey'] and verified['declarationReference'] == config['declaration'] and
            verified['originalFundingExpiresEpoch'] == authorize(config, durable, source_reader=source_reader), 'registrar declaration or original funding differs')
    for kind in ('control', 'task'):
        issued = verified['keys'][kind]['issued']
        require(config['credentials'][kind] == {name: issued[name] for name in ('secretArn', 'versionId')},
                'registrar cannot substitute an issued runtime credential')
    ref = verified['nativeBootstrap']
    original = durable.state['scenarioOperations'].get(ref['key'])
    require(isinstance(original, dict) and canonical_digest(original) == ref['sha256'] and
            original.get('producer') == 'private-native-bootstrap' and original.get('candidateKey') == config['candidateKey'],
            'registrar original bootstrap receipt differs')
    binding = original['value']['native']
    definition = ecs.describe_task_definition(taskDefinition=binding['taskDefinitionArn'])['taskDefinition']
    require(definition.get('taskDefinitionArn') == binding['taskDefinitionArn'] and
            definition.get('taskRoleArn') == native['taskRoleArn'] and
            definition.get('executionRoleArn') == native['executionRoleArn'] and
            len(definition.get('containerDefinitions', [])) == 1, 'registrar changed the original privileged bootstrap roles')
    container = definition['containerDefinitions'][0]
    root = config['credentials']['registrar']
    require(container.get('name') == 'native-bootstrap' and
            container.get('command') == ['python', 'tools/hosted_pilot/native_bootstrap.py'] and
            [row['valueFrom'] for row in container.get('secrets', []) if row['name'] == SECRET_ENVS['registrar']] ==
            [root['secretArn']+':::'+root['versionId']], 'registrar root credential is not the original immutable application admin injection')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def metadata_task():
    uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4', '')
    require(re.fullmatch(r'http://169\.254\.170\.2/v4/[A-Za-z0-9_-]+', uri), 'registrar requires native ECS metadata')
    with build_opener(NoRedirect).open(Request(uri+'/task'), timeout=10) as response:
        raw = response.read(65537)
    require(len(raw) <= 65536, 'registrar native metadata exceeds bounds')
    return json.loads(raw)['TaskARN']


def main():
    require(os.getuid() == 10001 and not sys.argv[1:], 'registrar requires its fixed reviewed nonroot entry')
    raw = os.environ.get(CONFIG_ENV, '')
    require(0 < len(raw.encode()) <= 32768, 'registrar configuration is missing or exceeds bounds')
    config = json.loads(raw)
    validate(config)
    from controller_handoff import sdk
    from controller_source import verify as verify_source
    from image_manifest import verify as verify_image
    from agent_e2e_state import DurableExecution, open_store
    from agent_e2e_runtime_configuration import load_declaration
    from native_runtime_registration import provision_reviewed
    source = json.loads(Path('/opt/honua-controller-source.json').read_text())
    require(Path.cwd().resolve() == Path(__file__).resolve().parents[2], 'registrar must execute its sealed source tree')
    verify_source(source, source['source_sha'], source['tree_id'], source['archive_sha256'])
    require(sdk('sts', config['region']).get_caller_identity()['Account'] == config['accountId'], 'registrar AWS account differs')
    ecs, s3, states, ecr = (sdk(service, config['region']) for service in ('ecs', 's3', 'stepfunctions', 'ecr'))
    def registry_read(service, action, *args):
        require((service, action) == ('ecr', 'batch-get-image') and len(args) == 8 and args[::2] ==
                ('--region', '--registry-id', '--repository-name', '--image-ids') and
                args[1] == config['region'] and args[3] == config['accountId'] and args[7].startswith('imageDigest='),
                'registrar registry observation differs')
        return ecr.batch_get_image(registryId=args[3], repositoryName=args[5], imageIds=[{'imageDigest': args[7].split('=', 1)[1]}])
    image_verifier = lambda image, observed: verify_image(image, observed, aws=registry_read)
    task_arn = metadata_task()
    native = verify_task(config, task_arn, ecs=ecs, image_verifier=image_verifier)
    durable = DurableExecution(open_store(config['state'], config['candidateKey']), '/work/native-runtime-registration-ledger.json')
    try:
        verified = None
        def guard():
            authorize(config, durable)
            require(verify_task(config, task_arn, ecs=ecs, image_verifier=image_verifier) == native,
                    'registrar native custody changed')
            if verified is not None:
                bind_original_credentials(config, verified, durable, native, ecs=ecs)
        guard()
        verified = load_declaration(config['declaration'], durable=durable, s3=s3, ecs=ecs, states=states, guard=guard)
        guard()
        # Preserve each actual carrier separately. A recovery task can observe the
        # original operation, but cannot replace its declaration or first receipt.
        key = 'native-runtime-registrar:'+canonical_digest({'configuration': config, 'taskArn': task_arn})[7:]
        row = {'producer': 'private-native-runtime-registrar', 'candidateKey': config['candidateKey'],
               'value': {'configurationDigest': canonical_digest(config), 'operation': copy.deepcopy(config['operation']),
                         'declaration': copy.deepcopy(config['declaration']), 'native': native, 'imageSource': source}}
        records = durable.state.setdefault('scenarioOperations', {})
        require(key not in records or records[key] == row, 'registrar original carrier receipt changed')
        if key not in records:
            records[key] = row
            durable._persist()
        provision_reviewed(durable, config['declaration'], s3=s3, ecs=ecs, states=states, guard=guard)
    finally:
        durable.close()
    print('Original native runtime registration retained. Controller activation and live qualification remain separate.')
