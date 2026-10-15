"""Validate controller-collected deployment observations, never worker claims.

The trusted producer collects native control-plane responses and binds the actual
endpoint it probes. This validator does not provision or discover infrastructure.
"""
import re
from datetime import datetime
from urllib.parse import urlsplit

from agent_e2e_pack import PackError


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def identity(value, campaign, target):
    require(set(value) == {'schemaVersion', 'target', 'componentBindingDigest', 'endpoint', 'observedAt', 'nativeResponse'},
            'invalid semantic deployment observation')
    require(type(value['schemaVersion']) is int and value['schemaVersion'] == 1 and value['target'] == target and
            value['componentBindingDigest'] == campaign['componentBindingDigest'], 'semantic deployment target/candidate differs')
    endpoint = urlsplit(value['endpoint'])
    require(bool(endpoint.hostname) and (endpoint.scheme == 'https' or
            (endpoint.scheme == 'http' and endpoint.hostname in ('localhost', '127.0.0.1', '::1'))) and
            not endpoint.username and not endpoint.password and not endpoint.query and not endpoint.fragment,
            'invalid observed deployment endpoint')
    def instant(text):
        result = datetime.fromisoformat(text.replace('Z', '+00:00'))
        require(result.tzinfo is not None, 'deployment observation requires timezone')
        return result
    require(instant(campaign['startedAt']) <= instant(value['observedAt']) <= instant(campaign['expiresAt']),
            'semantic deployment observation is outside campaign')
    native = value['nativeResponse']
    if target == 'local-docker':
        require(isinstance(native, list) and len(native) == 1, 'missing Docker inspect observation')
        container = native[0]
        require(re.fullmatch(r'[0-9a-f]{64}', container.get('Id', '')) and
                re.fullmatch(r'sha256:[0-9a-f]{64}', container.get('Image', '')) and
                container.get('State', {}).get('Running') is True, 'invalid running Docker identity')
        return 'docker:'+container['Id']
    require(isinstance(native, dict), 'missing native deployment response')
    if target == 'aws-ecs-redis-on':
        require(native.get('failures', []) == [] and isinstance(native.get('tasks'), list) and
                bool(native['tasks']), 'missing ECS task observations')
        identities = []
        for task in native['tasks']:
            arn = task.get('taskArn', '')
            require(re.fullmatch(r'arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task/[^/]+/[0-9a-f]{32}', arn) and
                    task.get('clusterArn') == arn.rsplit(':task/', 1)[0]+':cluster/'+arn.split('/')[-2] and
                    task.get('launchType') == 'FARGATE' and task.get('lastStatus') == 'RUNNING',
                    'invalid running Fargate identity')
            identities.append(arn)
        require(len(set(identities)) == len(identities), 'duplicate ECS deployment identity')
        return '|'.join(sorted(identities))
    if target == 'aws-eks':
        cluster, pod = native.get('cluster', {}), native.get('pod', {})
        require(re.fullmatch(r'arn:aws:eks:[a-z0-9-]+:[0-9]{12}:cluster/[^/]+', cluster.get('arn', '')) and
                cluster.get('status') == 'ACTIVE' and pod.get('kind') == 'Pod' and
                pod.get('status', {}).get('phase') == 'Running' and
                bool(pod.get('metadata', {}).get('uid')) and bool(pod.get('metadata', {}).get('namespace')),
                'missing active EKS cluster/pod observations')
        return cluster['arn']+'/'+pod['metadata']['uid']
    if target == 'aws-lambda':
        require(re.fullmatch(r'arn:aws:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9_-]+(?::[A-Za-z0-9_-]+)?',
                            native.get('FunctionArn', '')) and native.get('State') == 'Active' and
                bool(native.get('CodeSha256')), 'missing active Lambda function observation')
        return native['FunctionArn']
    require(target == 'aws-batch', 'unknown semantic deployment target')
    jobs = native.get('jobs', [])
    require(len(jobs) == 1 and re.fullmatch(r'arn:aws:batch:[a-z0-9-]+:[0-9]{12}:job/[a-z0-9-]+',
            jobs[0].get('jobArn', '')) and jobs[0].get('status') in ('RUNNING', 'SUCCEEDED'),
            'missing real Batch job observation')
    return jobs[0]['jobArn']


def bind(value, deployment_digest, endpoint):
    return {'deploymentDigest': deployment_digest, 'endpoint': endpoint, 'observation': value}


def unbind(value, deployment_digest, endpoint):
    require(set(value) == {'deploymentDigest', 'endpoint', 'observation'} and
            value['deploymentDigest'] == deployment_digest and value['endpoint'] == endpoint,
            'semantic evidence belongs to another deployment')
    return value['observation']
