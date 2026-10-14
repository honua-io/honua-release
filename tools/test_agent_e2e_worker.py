"""Transport isolation checks with service doubles, never cloud qualification."""
import copy

import pytest

from agent_e2e_pack import PackError
from agent_e2e_worker import Worker


CONFIG = {'imageId':'ami-123abc', 'instanceType':'t3.small', 'subnetId':'subnet-test',
          'securityGroupIds':['sg-test'], 'instanceProfileArn':'arn:aws:iam::123456789012:instance-profile/worker',
          'region':'us-east-1'}
CANDIDATE = 'sha256:'+'a'*64


class EC2:
    def __init__(self):
        self.calls = []
    def run_instances(self, **kwargs):
        self.calls.append(kwargs)
        self.instance = {'InstanceId':'i-test', 'ImageId':kwargs['ImageId'], 'SubnetId':kwargs['SubnetId'],
                         'SecurityGroups':[{'GroupId':g} for g in kwargs['SecurityGroupIds']],
                         'IamInstanceProfile':kwargs['IamInstanceProfile'], 'State':{'Name':'running'},
                         'Tags':kwargs['TagSpecifications'][0]['Tags']}
        return {'Instances':[self.instance]}
    def describe_instances(self, **kwargs):
        return {'Reservations':[{'Instances':[self.instance]}]}


class SSM:
    def __init__(self):
        self.sent = []
    def send_command(self, **kwargs):
        self.sent.append(kwargs)
        return {'Command':{'CommandId':'command-1'}}
    def get_command_invocation(self, **kwargs):
        return {'Status':'Success', 'ResponseCode':0, 'StandardOutputContent':'x'*20000}


def test_only_encoded_command_is_sent_to_unprivileged_remote_user():
    ec2, ssm = EC2(), SSM()
    worker = Worker(ec2, ssm, copy.deepcopy(CONFIG), clock=lambda:1000)
    identity = worker.launch('attempt-1', CANDIDATE, {'state':'reserved','kind':'infrastructure'}, 600)
    command = 'echo "$(secret)"; touch /somewhere'
    result = worker.execute(identity, command)
    script = ssm.sent[0]['Parameters']['commands'][0]
    assert command not in script
    assert 'runuser -u honua-agent -- timeout' in script
    assert 'test ! -S /var/run/docker.sock' in script
    assert len(result['stdout']) == 16384
    assert ec2.calls[0]['MetadataOptions']['HttpTokens'] == 'required'


def test_no_paid_launch_without_reservation():
    ec2 = EC2()
    worker = Worker(ec2, SSM(), copy.deepcopy(CONFIG))
    with pytest.raises(PackError, match='reserve'):
        worker.launch('attempt-1', CANDIDATE, {'state':'settled','kind':'infrastructure'}, 600)
    assert ec2.calls == []


def test_changed_instance_identity_blocks_commands():
    ec2, ssm = EC2(), SSM()
    worker = Worker(ec2, ssm, copy.deepcopy(CONFIG), clock=lambda:1000)
    identity = worker.launch('attempt-1', CANDIDATE, {'state':'reserved','kind':'infrastructure'}, 600)
    ec2.instance['IamInstanceProfile']['Arn'] = 'different-profile'
    with pytest.raises(PackError, match='identity'):
        worker.execute(identity, 'echo hello')
    assert ssm.sent == []


def test_command_cannot_outlive_reserved_worker_lifetime():
    ec2, ssm = EC2(), SSM()
    worker = Worker(ec2, ssm, copy.deepcopy(CONFIG), clock=lambda:1000)
    identity = worker.launch('attempt-1', CANDIDATE, {'state':'reserved','kind':'infrastructure'}, 60)
    with pytest.raises(PackError, match='bounds'):
        worker.execute(identity, 'echo hello', timeout=300)
    assert ssm.sent == []
