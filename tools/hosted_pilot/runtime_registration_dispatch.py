#!/usr/bin/env python3
"""Start or observe one original, quote-funded private registrar execution.

The permanent native claim precedes the only StartExecution call. Recovery is
observation-only, including a lost acknowledgement before AWS records a run.
No accounting lease, admission, reservation, credential or registration is made
by this dispatcher. The original admission and the independent reaper own the
absolute deadline; a new workflow cannot extend it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_state import StateStore
from agent_e2e_worker_lifecycle import terraform_definition_digest
from agent_e2e_runtime_configuration import source_file
from controller_archive import canonical, digest, require
from controller_budget import read_allowance
from controller_handoff import ROOT, git, sdk
from native_runtime_registration_carrier import COMMAND, CONFIG_ENV, SECRET_ENVS, validate
from registration_dispatch_admission import verify_original_dispatch_admission

PATH = 'certification/agent-e2e/runtime-registration-handoff.json'
WORKFLOW = 'honua-io/honua-release/.github/workflows/hosted-pilot-runtime-registration.yml@refs/heads/trunk'
FIELDS = {'schemaVersion', 'region', 'accountId', 'configuration', 'configurationSha256', 'taskDefinitionArn',
          'taskRoleArn', 'executionRoleArn', 'stateMachineArn', 'stateMachineBaseArn', 'stateMachineRoleArn',
          'definitionDigest', 'reaper', 'installation', 'dispatchBudget'}


def validate_handoff(value):
    require(isinstance(value, dict) and set(value) == FIELDS and value['schemaVersion'] == 1,
            'exact reviewed registration handoff required')
    cfg = value['configuration']; validate(cfg)
    require(value['configurationSha256'] == digest(canonical(cfg)) and
            all(value[name] == cfg[name] for name in ('region', 'accountId')),
            'registration source configuration changed')
    base = value['stateMachineBaseArn']
    prefix = cfg['taskDefinitionFamily'].split('/')[-1].removesuffix('-runtime-registration')
    require(base == 'arn:aws:states:'+cfg['region']+':'+cfg['accountId']+':stateMachine:'+prefix+'-runtime-registration' and
            re.fullmatch(re.escape(base)+r':[1-9][0-9]*', value['stateMachineArn']) and
            re.fullmatch(re.escape(cfg['taskDefinitionFamily'])+r':[1-9][0-9]*', value['taskDefinitionArn']),
            'registration requires immutable native versions')
    installation = value['installation']
    require(set(installation) == {'candidateKey', 'configurationDigest', 'originalFundingExpiresEpoch', 'maxExecutionSeconds',
            'clusterArn', 'taskDefinitionArn', 'claimTable', 'claimKey', 'startedBy', 'subnetIds', 'securityGroupIds'} and
            installation['configurationDigest'] == value['configurationSha256'] and
            installation['candidateKey'] == cfg['candidateKey'] and installation['clusterArn'] == cfg['clusterArn'] and
            installation['taskDefinitionArn'] == value['taskDefinitionArn'] and
            installation['maxExecutionSeconds'] == cfg['maxExecutionSeconds'] and
            installation['claimKey'] == 'runtime-registration:'+value['configurationSha256'][7:] and
            installation['startedBy'] == 'reg-'+value['configurationSha256'][7:39] and
            type(installation['originalFundingExpiresEpoch']) is int,
            'registration installation changed original identities or deadline')
    budget = value['dispatchBudget']
    require(isinstance(budget, dict) and set(budget) == {'state','candidateKey','generationDigest','reservationId','operationId','basisDigest','perDispatchCeilingUsd','maximumDispatches','claimTable'} and
            isinstance(budget['state'], dict) and set(budget['state']) == {'region','bucket','table'},
            'registration requires the original closed dispatch allowance')
    require(budget['state'] == cfg['state'] and budget['candidateKey'] == cfg['candidateKey'] and
            budget['generationDigest'] == cfg['generationDigest'] and budget['claimTable'] == installation['claimTable'] and
            budget['reservationId'] not in cfg['operation']['reservationIds'].values(),
            'registration cannot reuse effect rows for dispatch overhead')
    return cfg


def inspect_task_definition(value, ecs):
    cfg = value['configuration']
    response = ecs.describe_task_definition(taskDefinition=value['taskDefinitionArn'])
    task = response['taskDefinition']
    require(task.get('taskDefinitionArn') == value['taskDefinitionArn'] and task.get('status') == 'ACTIVE' and
            task.get('taskRoleArn') == value['taskRoleArn'] and task.get('executionRoleArn') == value['executionRoleArn'] and
            task.get('networkMode') == 'awsvpc' and task.get('requiresCompatibilities') == ['FARGATE'] and
            task.get('cpu') == '256' and task.get('memory') == '1024' and
            task.get('ephemeralStorage', {'sizeInGiB': 20}) == {'sizeInGiB': 20} and
            task.get('runtimePlatform') == {'cpuArchitecture':'X86_64', 'operatingSystemFamily':'LINUX'} and
            len(task.get('containerDefinitions', [])) == 1, 'registrar task revision, resource bounds or roles differ')
    container = task['containerDefinitions'][0]
    require(container.get('name') == 'runtime-registration' and container.get('image') == cfg['imageUri'] and
            container.get('command') == COMMAND and container.get('user') == '10001:10001' and
            container.get('readonlyRootFilesystem') is True and container.get('essential') is True and
            not container.get('privileged', False) and not container.get('entryPoint') and not container.get('portMappings') and
            not container.get('environmentFiles') and not container.get('volumesFrom') and
            container.get('linuxParameters') == {'capabilities':{'drop':['ALL']}} and
            container.get('mountPoints') == [{'sourceVolume':'work','containerPath':'/work','readOnly':False}],
            'registrar container command or isolation differs')
    environment = container.get('environment', [])
    require(len(environment) == 1 and environment[0].get('name') == CONFIG_ENV and
            json.loads(environment[0]['value']) == cfg, 'registrar configuration injection differs')
    secrets = container.get('secrets', [])
    require(len(secrets) == 3 and {row['name']:row['valueFrom'] for row in secrets} == {
        SECRET_ENVS[kind]:ref['secretArn']+':::'+ref['versionId'] for kind,ref in cfg['credentials'].items()},
        'registrar must receive exactly three original immutable private values')
    return task


def inspect_lifecycle(value, states, scheduler):
    expected = value['installation']
    def machine(arn, role, pinned):
        original = states.describe_state_machine(stateMachineArn=arn)
        require(original.get('type') == 'STANDARD' and original.get('roleArn') == role and
                original.get('status') == 'ACTIVE' and terraform_definition_digest(original['definition']) == pinned,
                'registration published lifecycle source or role differs')
        definition = json.loads(original['definition'])
        require(definition.get('QueryLanguage') == 'JSONata' and
                definition['States']['Installation']['Output']['installation'] == expected and
                not any(row.get('Retry') for row in definition['States'].values()),
                'registration lifecycle installation or retry authority differs')
        return original
    lifecycle = machine(value['stateMachineArn'], value['stateMachineRoleArn'], value['definitionDigest'])
    reaper = value['reaper']
    require(set(reaper) == {'stateMachineArn','stateMachineRoleArn','definitionDigest','scheduleGroup','scheduleName','roleArn','endDate'} and
            re.fullmatch(re.escape(value['stateMachineBaseArn']+'-reaper')+r':[1-9][0-9]*', reaper['stateMachineArn']),
            'original independent reaper version is required')
    native_reaper = machine(reaper['stateMachineArn'], reaper['stateMachineRoleArn'], reaper['definitionDigest'])
    actual = scheduler.get_schedule(GroupName=reaper['scheduleGroup'], Name=reaper['scheduleName'])
    end = actual.get('EndDate')
    require(isinstance(end, datetime) and end.tzinfo is not None and
            end.astimezone(timezone.utc).isoformat().replace('+00:00','Z') == reaper['endDate'] and
            actual.get('State') == 'ENABLED' and actual.get('ScheduleExpression') == 'rate(1 minute)' and
            actual.get('ScheduleExpressionTimezone') == 'UTC' and actual.get('FlexibleTimeWindow') == {'Mode':'OFF'} and
            actual.get('Target') == {'Arn':reaper['stateMachineArn'],'RoleArn':reaper['roleArn'],'Input':'{}',
                                    'RetryPolicy':{'MaximumEventAgeInSeconds':60,'MaximumRetryAttempts':0}},
            'independent registration clock is absent or changed')
    return {'lifecycle':lifecycle, 'reaper':native_reaper, 'schedule':actual}


def claim_once(value, verified, *, db, overhead_head):
    cfg, installation = value['configuration'], value['installation']
    admitted = verified['admission']
    require(admitted['originalFundingExpiresEpoch'] == installation['originalFundingExpiresEpoch'],
            'registrar installation cannot extend original funding')
    head = verified['head']
    require(all(overhead_head.get(name) == field for name,field in head.items()),
            'registration dispatch overhead and admission observed different original heads')
    request = {'schemaVersion':1,'configurationDigest':value['configurationSha256'],
               'admissionDigest':digest(canonical(admitted)), 'deadlineEpoch':admitted['deadlineEpoch']}
    name = 'registration-'+value['configurationSha256'][7:]
    execution = value['stateMachineBaseArn'].replace(':stateMachine:',':execution:')+':'+name
    row = {'claimKey':{'S':installation['claimKey']}, 'configurationDigest':{'S':value['configurationSha256']},
           'admissionDigest':{'S':request['admissionDigest']}, 'candidateKey':{'S':cfg['candidateKey']},
           'admittedEpoch':{'N':str(admitted['admittedEpoch'])}, 'deadlineEpoch':{'N':str(admitted['deadlineEpoch'])},
           'executionArn':{'S':execution}, 'inputDigest':{'S':digest(canonical(request))},
           'input':{'S':canonical(request).decode()}, 'stateMachineArn':{'S':value['stateMachineArn']},
           'taskDefinitionArn':{'S':value['taskDefinitionArn']}, 'sourceHead':{'S':canonical(head).decode()}}
    key = {'claimKey':row['claimKey']}
    existing = db.get_item(TableName=installation['claimTable'], Key=key, ConsistentRead=True).get('Item')
    if existing:
        require(all(existing.get(name) == field for name,field in row.items() if name != 'sourceHead'),
                'original permanent registration claim differs; never replace it')
        return False, request, execution, existing
    budget = value['dispatchBudget']
    counter = digest(canonical({name:budget[name] for name in ('candidateKey','generationDigest','reservationId')}))
    fields = list(head)
    db.transact_write_items(TransactItems=[
        {'ConditionCheck':{'TableName':cfg['state']['table'],'Key':{'candidateKey':{'S':cfg['candidateKey']}},
            'ConditionExpression':' AND '.join(name+' = :h'+str(i) for i,name in enumerate(fields)),
            'ExpressionAttributeValues':{':h'+str(i):head[name] for i,name in enumerate(fields)}}},
        {'Update':{'TableName':installation['claimTable'],'Key':{'claimKey':{'S':'capacity:'+counter}},
            'ConditionExpression':'(attribute_not_exists(configDigest) OR configDigest = :config) AND (attribute_not_exists(used) OR used < :maximum)',
            'UpdateExpression':'SET configDigest = :config ADD used :one',
            'ExpressionAttributeValues':{':config':{'S':digest(canonical(budget))},':maximum':{'N':str(budget['maximumDispatches'])},':one':{'N':'1'}}}},
        {'Put':{'TableName':installation['claimTable'],'Item':row,'ConditionExpression':'attribute_not_exists(claimKey)'}}])
    return True, request, execution, row


def observed_execution(states, value, request, execution):
    observed = states.describe_execution(executionArn=execution)
    require(observed.get('executionArn') == execution and observed.get('stateMachineVersionArn') == value['stateMachineArn'] and
            observed.get('stateMachineArn') == value['stateMachineBaseArn'] and observed.get('redriveCount',0) == 0 and
            observed.get('inputDetails',{}).get('included') is True and observed.get('input') == canonical(request).decode(),
            'original registration execution input, version or redrive differs')
    return observed


def original_task(states, ecs, value, execution, request, *, retain_history=lambda pages: None):
    pages = []; token = None
    while True:
        args = {'executionArn':execution,'includeExecutionData':True,'maxResults':1000}
        if token: args['nextToken'] = token
        page = states.get_execution_history(**args); pages.append(page)
        require(sum(len(p.get('events',[])) for p in pages) <= 10000 and len(pages) <= 12,
                'registration original execution history exceeds reviewed bound')
        token = page.get('nextToken')
        if not token: break
    retain_history(pages)
    events = [event for page in pages for event in page.get('events',[])]
    launches = [event['stateExitedEventDetails'] for event in events if event.get('type') == 'TaskStateExited' and
                event.get('stateExitedEventDetails',{}).get('name') == 'RunRegistrar']
    require(len(launches) == 1 and launches[0].get('outputDetails',{}).get('truncated') is False,
            'original registration sole task launch is unknown; never relaunch')
    launched = json.loads(launches[0]['output'])['launched']
    require(not launched.get('Failures') and len(launched.get('Tasks',[])) == 1 and
            launched['Tasks'][0]['TaskDefinitionArn'] == value['taskDefinitionArn'] and
            launched['Tasks'][0]['ClusterArn'] == value['configuration']['clusterArn'], 'original registration task output differs')
    arn = launched['Tasks'][0]['TaskArn']
    observed = ecs.describe_tasks(cluster=value['configuration']['clusterArn'],tasks=[arn],include=['TAGS'])
    require(not observed.get('failures') and len(observed.get('tasks',[])) == 1, 'registration task cannot be positively observed')
    task = observed['tasks'][0]
    tags = {row['key']:row['value'] for row in task.get('tags',[])}
    require(task.get('taskArn') == arn and task.get('clusterArn') == value['configuration']['clusterArn'] and
            task.get('taskDefinitionArn') == value['taskDefinitionArn'] and task.get('startedBy') == value['installation']['startedBy'] and
            task.get('lastStatus') == 'STOPPED' and task.get('launchType') == 'FARGATE' and task.get('enableExecuteCommand') is False and
            all(tags.get(key) == expected for key,expected in {'honua-registration':value['configurationSha256'],
                'honua-registration-admission':request['admissionDigest'],
                'honua-registration-candidate':value['configuration']['candidateKey']}.items()) and
            len(task.get('containers',[])) == 1 and task['containers'][0].get('name') == 'runtime-registration' and
            task['containers'][0].get('image') == value['configuration']['imageUri'] and
            type(task['containers'][0].get('exitCode')) is int, 'original registration task binding or stopped observation differs')
    require(not task.get('overrides',{}).get('containerOverrides') or
            all(set(row) <= {'name'} and row.get('name') == 'runtime-registration'
                for row in task['overrides']['containerOverrides']), 'registration task overrides differ')
    return task, pages


def dispatch(value, *, store, ecs, states, scheduler, source_reader=source_file, clock=time.time, sleep=time.sleep, retain):
    cfg = validate_handoff(value)
    inspected = inspect_lifecycle(value, states, scheduler)
    definition = inspect_task_definition(value, ecs)
    verified = verify_original_dispatch_admission(cfg, store, source_reader=source_reader, clock=clock)
    overhead = read_allowance(value['dispatchBudget'], store.s3, store.db)
    # The permanent native claim and capacity use are atomic with the actual
    # authenticated seven-field accounting head. No fake live lease is used.
    created, request, execution, claim = claim_once(value, verified, db=store.db, overhead_head=overhead)
    metadata = {'schema':'honua.private-runtime-registration-dispatch/v1','candidateKey':cfg['candidateKey'],
        'configurationDigest':value['configurationSha256'],'executionArn':execution,'input':request,
        'claim':claim,'activation':False,'qualification':False,'status':'claimed-effects-unknown'}
    retain(metadata)
    if created:
        require(clock() < request['deadlineEpoch'], 'original registration deadline passed before launch; retain claim')
        try:
            states.start_execution(stateMachineArn=value['stateMachineArn'], name=execution.rsplit(':',1)[1], input=canonical(request).decode())
        except Exception:
            # Do not classify an SDK exception as nonacceptance or retry Start.
            # The permanent execution name permits only native observation.
            pass
    deadline = min(request['deadlineEpoch']+120, value['installation']['originalFundingExpiresEpoch']+120)
    while clock() < deadline:
        original = observed_execution(states, value, request, execution)
        if original['status'] != 'RUNNING':
            metadata['nativeExecution'] = original
            retain(metadata)
            def retain_history(pages):
                metadata['originalHistory'] = pages
                retain(metadata)
            task, history = original_task(states, ecs, value, execution, request, retain_history=retain_history)
            metadata.update(status='native-terminal-observed', nativeExecution=original, nativeTask=task,
                            originalHistory=history, nativeDefinitions=inspected, taskDefinition=definition)
            retain(metadata)
            require(original['status'] == 'SUCCEEDED' and task['containers'][0]['exitCode'] == 0,
                    'registrar outcome failed or unknown; native registration and costs remain unresolved')
            return metadata
        sleep(3)
    raise ValueError('original registration deadline exhausted; independent reaper owns stop and original liabilities remain')


def main():
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    require(os.environ.get('GITHUB_REPOSITORY') == 'honua-io/honua-release' and os.environ.get('GITHUB_REF') == 'refs/heads/trunk' and
            os.environ.get('GITHUB_WORKFLOW_REF') == WORKFLOW and git('rev-parse','HEAD').decode().strip() == os.environ.get('GITHUB_SHA'),
            'registration dispatcher requires exact protected workflow source')
    raw = git('show',os.environ['GITHUB_SHA']+':'+PATH); git('ls-files','--error-unmatch','--',PATH)
    require((ROOT/PATH).is_file() and not (ROOT/PATH).is_symlink() and (ROOT/PATH).read_bytes() == raw,
            'reviewed registrar handoff is absent or changed')
    value = json.loads(raw); validate_handoff(value)
    region = value['region']
    require(sdk('sts',region).get_caller_identity()['Account'] == value['accountId'], 'registration account differs')
    s3, db, ecs, states, scheduler = (sdk(name,region) for name in ('s3','dynamodb','ecs','stepfunctions','scheduler'))
    store = StateStore(s3=s3,dynamodb=db,bucket=value['configuration']['state']['bucket'],
        table=value['configuration']['state']['table'],candidate_key=value['configuration']['candidateKey'])
    def retain(metadata):
        metadata = metadata | {'producer':{'repository':os.environ['GITHUB_REPOSITORY'],
            'sourceSha':os.environ['GITHUB_SHA'],'workflow':WORKFLOW,
            'runId':int(os.environ['GITHUB_RUN_ID']),'runAttempt':int(os.environ['GITHUB_RUN_ATTEMPT'])}}
        def native(value):
            if isinstance(value,datetime) and value.tzinfo is not None: return value.isoformat()
            raise TypeError('unsupported native dispatch metadata')
        args.output.parent.mkdir(parents=True,exist_ok=True)
        temporary = args.output.with_suffix('.tmp')
        temporary.write_text(json.dumps(metadata,sort_keys=True,separators=(',',':'),default=native)+'\n')
        temporary.replace(args.output)
    dispatch(value,store=store,ecs=ecs,states=states,scheduler=scheduler,retain=retain)
    print('Original private registrar task stopped; registration activation and cost settlement remain independent.')


if __name__ == '__main__':
    try: main()
    except Exception as error:
        code = getattr(error,'response',{}).get('Error',{}).get('Code')
        print('registration dispatch refused; preserve original claim, deadline and liabilities'+
              (': AWS '+code if isinstance(code,str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',code) else ''),file=sys.stderr)
        sys.exit(1)
