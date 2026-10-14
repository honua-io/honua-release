"""Disposable EC2/SSM transport for model-selected terminal actions.

This is a transport, not an outcome oracle or a complete scenario backend. Images,
network policy and the SSM-only instance profile must be reviewed controller
inputs. Task instances must not have controller storage, provisioning or approver
permissions. The independent watchdog owns expired-instance cleanup.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time

from agent_e2e_pack import PackError


class Worker:
    def __init__(self, ec2, ssm, config, *, clock=time.time, sleep=time.sleep):
        self.ec2, self.ssm, self.config = ec2, ssm, config
        self.clock, self.sleep = clock, sleep
        required = {'imageId', 'instanceType', 'subnetId', 'securityGroupIds', 'instanceProfileArn', 'region'}
        if set(config) != required or not re.fullmatch(r'ami-[0-9a-f]+', config['imageId']):
            raise PackError('worker requires reviewed pinned configuration')
        if not config['securityGroupIds'] or not config['instanceProfileArn']:
            raise PackError('missing isolated worker network/identity')

    def launch(self, namespace, candidate, reservation, lifetime_seconds):
        if (not re.fullmatch(r'[A-Za-z0-9-]{1,64}', namespace) or not re.fullmatch(r'sha256:[0-9a-f]{64}', candidate)
                or type(lifetime_seconds) is not int or not 60 <= lifetime_seconds <= 6000):
            raise PackError('invalid worker lifetime or identity')
        if reservation.get('state') != 'reserved' or reservation.get('kind') != 'infrastructure':
            raise PackError('reserve infrastructure liability before launching worker')
        token = hashlib.sha256((candidate+'\n'+namespace).encode()).hexdigest()
        tags = [{'Key':'honua-agent-attempt','Value':namespace}, {'Key':'honua-agent-candidate','Value':candidate},
                {'Key':'honua-agent-expires','Value':str(int(self.clock())+lifetime_seconds)}]
        response = self.ec2.run_instances(ImageId=self.config['imageId'], InstanceType=self.config['instanceType'],
            MinCount=1, MaxCount=1, ClientToken=token, SubnetId=self.config['subnetId'],
            SecurityGroupIds=self.config['securityGroupIds'],
            IamInstanceProfile={'Arn':self.config['instanceProfileArn']},
            MetadataOptions={'HttpTokens':'required','HttpPutResponseHopLimit':1,'HttpEndpoint':'enabled'},
            TagSpecifications=[{'ResourceType':'instance','Tags':tags}, {'ResourceType':'volume','Tags':tags}],
            InstanceInitiatedShutdownBehavior='terminate')
        if len(response['Instances']) != 1:
            raise PackError('worker launch returned unexpected inventory')
        return {'instanceId':response['Instances'][0]['InstanceId'], 'namespace':namespace, 'candidate':candidate}

    def verify_identity(self, worker):
        response = self.ec2.describe_instances(InstanceIds=[worker['instanceId']])
        rows = [i for r in response['Reservations'] for i in r['Instances']]
        if len(rows) != 1:
            raise PackError('worker identity missing')
        instance = rows[0]
        tags = {t['Key']:t['Value'] for t in instance.get('Tags', [])}
        if (tags.get('honua-agent-attempt') != worker['namespace'] or tags.get('honua-agent-candidate') != worker['candidate']
                or instance['ImageId'] != self.config['imageId'] or instance['SubnetId'] != self.config['subnetId']
                or {g['GroupId'] for g in instance.get('SecurityGroups', [])} != set(self.config['securityGroupIds'])
                or instance.get('IamInstanceProfile', {}).get('Arn') != self.config['instanceProfileArn']):
            raise PackError('worker identity/configuration changed')
        return instance

    def execute(self, worker, command, timeout=300):
        instance = self.verify_identity(worker)
        if instance['State']['Name'] != 'running':
            raise PackError('worker is not running')
        expires = int(next(t['Value'] for t in instance['Tags'] if t['Key']=='honua-agent-expires'))
        if (not isinstance(command, str) or not command or len(command.encode()) > 65536
                or type(timeout) is not int or not 1 <= timeout <= 300 or self.clock()+timeout > expires):
            raise PackError('terminal action exceeds worker bounds')
        encoded = base64.b64encode(command.encode()).decode('ascii')
        # Only base64 and a checked integer enter controller-generated shell.
        # The model-selected command executes as an unprivileged VM user, never
        # in the controller or under the SSM root identity.
        script = ("set -eu\n"
                  "id honua-agent >/dev/null\n"
                  "test ! -S /var/run/docker.sock\n"
                  f"printf '%s' '{encoded}' | base64 -d | "
                  f"runuser -u honua-agent -- timeout --kill-after=5 {timeout} bash -s\n")
        response = self.ssm.send_command(InstanceIds=[worker['instanceId']], DocumentName='AWS-RunShellScript',
            TimeoutSeconds=max(30, timeout+10), Parameters={'commands':[script], 'executionTimeout':[str(timeout+5)]})
        command_id = response['Command']['CommandId']
        deadline = min(expires, self.clock()+timeout+20)
        while self.clock() < deadline:
            try:
                result = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=worker['instanceId'])
            except Exception as exc:
                code = getattr(exc, 'response', {}).get('Error', {}).get('Code')
                if code != 'InvocationDoesNotExist':
                    raise
                self.sleep(1)
                continue
            if result['Status'] in ('Pending','InProgress','Delayed','Cancelling'):
                self.sleep(1)
                continue
            return {'commandId':command_id, 'status':result['Status'], 'exitCode':result.get('ResponseCode'),
                    'stdout':result.get('StandardOutputContent','')[:16384],
                    'stderr':result.get('StandardErrorContent','')[:4096]}
        self.ssm.cancel_command(CommandId=command_id, InstanceIds=[worker['instanceId']])
        raise PackError('worker command did not terminate within observed lifetime')

    def terminate(self, worker):
        self.verify_identity(worker)
        self.ec2.terminate_instances(InstanceIds=[worker['instanceId']])
        self.ec2.get_waiter('instance_terminated').wait(InstanceIds=[worker['instanceId']],
            WaiterConfig={'Delay':5,'MaxAttempts':60})
        return {'instanceId':worker['instanceId'], 'state':'terminated'}
