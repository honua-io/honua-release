"""Native identity fixtures are contract tests, never deployment evidence."""
import copy

import pytest

from agent_e2e_deployment import identity
from agent_e2e_pack import PackError

TARGETS = ['local-docker', 'aws-ecs-redis-on', 'aws-eks', 'aws-lambda', 'aws-batch']
CAMPAIGN = {'componentBindingDigest':'sha256:'+'f'*64,
            'startedAt':'2026-10-15T10:00:00Z', 'expiresAt':'2026-10-15T11:00:00Z'}


def deployment(target, campaign):
    native = {
        'local-docker':[{'Id':'a'*64, 'Image':'sha256:'+'b'*64, 'State':{'Running':True}}],
        'aws-ecs-redis-on':{'tasks':[{'taskArn':'arn:aws:ecs:us-east-1:123456789012:task/semantic/'+'a'*32,
            'clusterArn':'arn:aws:ecs:us-east-1:123456789012:cluster/semantic',
            'launchType':'FARGATE', 'lastStatus':'RUNNING'}], 'failures':[]},
        'aws-eks':{'cluster':{'arn':'arn:aws:eks:us-east-1:123456789012:cluster/semantic', 'status':'ACTIVE'},
            'pod':{'kind':'Pod', 'metadata':{'uid':'observed-pod-uid','namespace':'semantic'}, 'status':{'phase':'Running'}}},
        'aws-lambda':{'FunctionArn':'arn:aws:lambda:us-east-1:123456789012:function:semantic', 'State':'Active', 'CodeSha256':'observed-code'},
        'aws-batch':{'jobs':[{'jobArn':'arn:aws:batch:us-east-1:123456789012:job/observed-job-id', 'status':'SUCCEEDED'}]},
    }[target]
    return {'schemaVersion':1, 'target':target, 'componentBindingDigest':campaign['componentBindingDigest'],
            'endpoint':f'https://{target}.example', 'observedAt':campaign['startedAt'], 'nativeResponse':native}


@pytest.mark.parametrize('target', TARGETS)
def test_native_identity_is_bound_to_target_candidate_and_campaign(target):
    observed = deployment(target, CAMPAIGN)
    assert identity(observed, CAMPAIGN, target)
    wrong = copy.deepcopy(observed)
    wrong['componentBindingDigest'] = 'sha256:'+'0'*64
    with pytest.raises(PackError, match='target/candidate'):
        identity(wrong, CAMPAIGN, target)
    wrong = copy.deepcopy(observed)
    wrong['observedAt'] = '2026-10-14T10:00:00Z'
    with pytest.raises(PackError, match='outside campaign'):
        identity(wrong, CAMPAIGN, target)


@pytest.mark.parametrize('target', TARGETS[1:])
def test_relabelled_docker_identity_cannot_become_cloud_evidence(target):
    observed = deployment('local-docker', CAMPAIGN)
    observed['target'] = target
    with pytest.raises(PackError, match='native deployment'):
        identity(observed, CAMPAIGN, target)
