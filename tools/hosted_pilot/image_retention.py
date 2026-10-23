"""Verify actual finite ECR expiry policy before immutable worker publication."""
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import canonical_digest
from controller_archive import require

EXPIRY_DAYS = 14

def policy():
    return {'rules': [
        {'rulePriority': 1, 'description': 'Expire untagged worker artifacts after one day',
         'selection': {'tagStatus': 'untagged', 'countType': 'sinceImagePushed', 'countUnit': 'days', 'countNumber': 1}, 'action': {'type': 'expire'}},
        {'rulePriority': 2, 'description': 'Expire immutable worker artifacts after fourteen days',
         'selection': {'tagStatus': 'any', 'countType': 'sinceImagePushed', 'countUnit': 'days', 'countNumber': EXPIRY_DAYS}, 'action': {'type': 'expire'}}]}

def verify(repository, account, region, *, call):
    response = json.loads(call(['aws', 'ecr', 'get-lifecycle-policy', '--region', region,
        '--registry-id', account, '--repository-name', repository, '--output', 'json'], capture=True))
    require(response.get('registryId') == account and response.get('repositoryName') == repository and
        json.loads(response.get('lifecyclePolicyText', '{}')) == policy(),
        'actual worker registry lacks its exact finite immutable/untagged artifact expiry policy')
    return {'policySha256': canonical_digest(policy()), 'expiryEligibilityDays': EXPIRY_DAYS,
            'untaggedExpiryEligibilityDays': 1, 'repository': repository, 'registryId': account,
            'source': 'ecr:GetLifecyclePolicy', 'deletionTiming': 'ECR service-managed asynchronous expiry; no archive or task history deletion'}
