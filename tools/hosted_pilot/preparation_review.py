#!/usr/bin/env python3
"""Project actual private setup IDs for source review; never edits expectations."""
import argparse
import json
from pathlib import Path
import re
from controller_archive import canonical, require


def project(result, outcome):
    require(result['schema'] == 'honua.private-controller-preparation-result/v1' and
            result['qualification'] is False and result['requiresSourceReviewBeforeFreeze'] is True and
            outcome['status'] == 'completed' and outcome['exitCode'] == 0 and
            result['producerTaskArn'] == outcome['taskArn'] and
            result['candidateKey'] == outcome['invocation']['candidateKey'] and result['operation'] == outcome['invocation']['operation'],
            'native preparation result does not match its verified native outcome')
    value = result['result']
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', value['key']) if result['operation'] == 'prepare-studio-fixture' else
            re.fullmatch(r'sha256:[a-f0-9]{64}/completed', value['key']), 'preparation state reference differs')
    require(re.fullmatch(r'sha256:[a-f0-9]{64}', value['sha256']), 'preparation state digest missing')
    review = {'schema': 'honua.private-preparation-source-review/v1', 'operation': result['operation'],
        'candidateKey': result['candidateKey'], 'sourceConfiguration': result['sourceConfiguration'],
        'nativeOutcome': outcome['outcomeObject'], 'producerTaskArn': outcome['taskArn'],
        'retainedState': {'key': value['key'], 'sha256': value['sha256']}, 'qualification': False,
        'requiresSourceReviewBeforeFreeze': True}
    if result['operation'] == 'prepare-native-context':
        context = value['value']
        review['agentContext'] = context['agentContext']
        review['ownerPrincipalId'] = context['ownerPrincipalId']
        review['agentApiKeyId'] = context['issuedAgentKey']['apiKeyId']
    else:
        fixture = value['fixture']
        review['fixture'] = {key: fixture[key] for key in ('itemId', 'draftId', 'versionId', 'contentHash', 'namespace', 'ownerPrincipalId')}
    return review


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('result', 'outcome', 'output'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    require(args.result.is_file() and not args.result.is_symlink() and args.outcome.is_file() and not args.outcome.is_symlink(), 'verified private preparation files missing')
    args.output.write_bytes(canonical(project(json.loads(args.result.read_text()), json.loads(args.outcome.read_text())))+b'\n')


if __name__ == '__main__':
    main()
