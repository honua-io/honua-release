#!/usr/bin/env python3
"""Custody-first foundation bootstrap; never upload plans, state or logs to GitHub."""
from __future__ import annotations
import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

from deploy import TF, ROOT, require, digest, private_json, run, aws, summarize

CUSTODY = {
    'aws_kms_key.custody', 'aws_s3_bucket.custody',
    'aws_s3_bucket_public_access_block.custody', 'aws_s3_bucket_ownership_controls.custody',
    'aws_s3_bucket_versioning.custody', 'aws_s3_bucket_server_side_encryption_configuration.custody',
    'aws_s3_bucket_policy.custody', 'aws_s3_bucket_lifecycle_configuration.plans',
}


IDENTITY_TAGS = {'foundation_source_sha': 'BootstrapFoundationSha',
                 'release_root_sha': 'BootstrapReleaseSha', 'config_path': 'BootstrapConfigPath',
                 'config_sha256': 'BootstrapConfigSha256', 'provider_lock_sha256': 'BootstrapLockSha256'}


def resolve_image_funding(cfg, source_root, source_sha, *, config_path=None):
    """Bind candidate-independent image funding to actual checked workflow source."""
    cfg = copy.deepcopy(cfg)
    fixed_input = 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
    if config_path == fixed_input:
        require(cfg.get('image_preparation_funding') is True, 'fixed image input requires its reviewed funding compiler mode')
    if 'image_preparation_funding' not in cfg:
        return cfg
    require(cfg['image_preparation_funding'] is True and re.fullmatch(r'[a-f0-9]{40}', source_sha),
            'image preparation funding requires the actual reviewed release source')
    ledger = cfg['budget_ledger']
    require(ledger['source_sha'] is None and ledger['config_sha256'] is None,
            'source-resolved image funding cannot replace an existing source/config binding')
    original_input = json.loads(run(['git', 'show', source_sha+':certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json']))
    def canonical_input(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    require(canonical_input(cfg) == canonical_input(original_input),
            'complete bootstrap input differs from the exact reviewed release source')
    cfg.pop('image_preparation_funding')
    relative = 'certification/hosted-pilot/deployments/foss4g-image-preparation.tfvars.json'
    path = Path(source_root)/relative
    require(path.is_file() and not path.is_symlink(), 'tracked image funding configuration missing')
    original = run(['git', 'show', source_sha+':'+relative])
    require(path.read_bytes() == original, 'image funding differs from the exact workflow source')
    value = json.loads(original)
    require(set(value) == {'region', 'name_prefix', 'owner', 'expires_at'} and
            all(value[key] == cfg[key] for key in value), 'image funding source differs from reviewed foundation identity')
    basis = json.loads(run(['git', 'show', source_sha+':certification/hosted-pilot/foundation-funding-basis.json']))
    required_reserve = Decimal(basis['priorUnknownUsageHold']['amountUsd'])+Decimal(basis['usageHoldExtension']['amountUsd'])
    required_reserve += sum(Decimal(row['amountUsd']) for row in basis['additionalReservedLiabilities'])
    require(basis['schema'] == 'honua.hosted-pilot-foundation-funding-basis/v1' and
            basis['accountId'] == cfg['account_id'] and basis['region'] == cfg['region'] and
            basis['accountZeroSpend'] is False and basis['originalHistoryMayBeReset'] is False and
            required_reserve == Decimal(basis['reservedUsd']) and required_reserve > 0 and
            Decimal(str(ledger['reserved_usd'])) >= required_reserve and
            0 < Decimal(str(ledger['deployment_reservation_usd'])) <= Decimal(basis['nextPublicationReservationUsd']) and
            Decimal(str(ledger['total_budget_usd'])) == Decimal(basis['totalBudgetUsd']) == 2000 and
            Decimal(str(ledger['cleanup_reserve_usd'])) == Decimal(basis['protectedCleanupUsd']) == 200,
            'foundation funding cannot discard original nonzero usage, legacy or cleanup reservations')
    require(ledger.get('reservation_id') == basis['nextPublicationReservationId'] and
            re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{2,119}', ledger['reservation_id']),
            'source compilation cannot mint a new original publication reservation')
    ledger['source_sha'] = source_sha
    ledger['config_sha256'] = hashlib.sha256(original).hexdigest()
    return cfg


def creation_tags(identity, run_id):
    require(re.fullmatch(r'[0-9]+', run_id), 'invalid bootstrap creation run')
    require(set(identity) == set(IDENTITY_TAGS), 'incomplete bootstrap creation identity')
    require(all(isinstance(value, str) and 0 < len(value) <= 256 for value in identity.values()), 'invalid bootstrap creation identity values')
    return {'ManagedBy': 'honua-release-hosted-pilot-bootstrap', 'BootstrapExecution': run_id,
            **{tag: identity[key] for key, tag in IDENTITY_TAGS.items()}}


def producer_identity():
    producer = json.loads((Path(os.environ['RUNNER_TEMP']) / 'recovery-producer.json').read_text())
    require(str(producer['id']) == os.environ['PILOT_RECOVERY_RUN_ID'], 'recovery producer run differs')
    return producer


def validate_recovery(value, identity):
    require(value['schema'] == 'honua.hosted-pilot-custody-recovery/v1' and recovery_compatible(value, identity), 'custody recovery source/config identity mismatch; a reviewed compatibility mapping is required for a fixed helper')
    producer = producer_identity()
    require(producer['head_sha'] == value['foundation_source_sha'] and str(producer['id']) == value['bootstrap_run_id'], 'journal source differs from its observed producing run')
    require(value.get('cloud_discovery_complete') is True, 'initial custody discovery incomplete; do not discard retained cloud resources or state')
    require(value['resources'], 'no imported custody identities; retain creation liability')


def recovery_prefix(cfg, run_id):
    return f"honua/hosted-pilot/{cfg['name_prefix']}/foundation-recovery/{run_id}/"


def discover_customer_key_tags(key_id):
    """Exclude only a positively described AWS-managed key from custody discovery."""
    answer = aws('kms', 'describe-key', '--key-id', key_id)
    metadata = answer.get('KeyMetadata') if isinstance(answer, dict) else None
    require(isinstance(metadata, dict) and metadata.get('KeyId') == key_id and
            metadata.get('AWSAccountId') == os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID'] and
            metadata.get('KeyManager') in ('AWS', 'CUSTOMER'),
            'KMS discovery requires actual same-account key identity and manager metadata')
    if metadata['KeyManager'] == 'AWS':
        return None
    return {tag['TagKey']: tag['TagValue'] for tag in
            aws('kms', 'list-resource-tags', '--key-id', key_id).get('Tags', [])}


def retrieve_recovery(binding, cfg, identity, work):
    """Recover independently of replaceable GitHub artifacts, including key-only failures."""
    run_id = os.environ.get('PILOT_RECOVERY_RUN_ID', '')
    if not run_id:
        return None
    producer = producer_identity()
    # Once secure custody existed, its immutable versioned journals survive both
    # native job reruns and later artifact retention/deletion.
    if exists('head-bucket', '--bucket', binding['bucket']):
        try:
            secured = secure_backend(binding)
        except (ValueError, KeyError):
            secured = None
        if secured:
            objects = aws('s3api', 'list-object-versions', '--bucket', secured['bucket'],
                          '--prefix', recovery_prefix(cfg, run_id) + 'journals/').get('Versions', [])
            for obj in sorted(objects, key=lambda item: item['LastModified'], reverse=True):
                require(obj.get('VersionId') not in (None, 'null') and re.fullmatch(re.escape(recovery_prefix(cfg, run_id)) + r'journals/[a-f0-9]{64}\.json', obj['Key']), 'invalid durable recovery journal identity')
                target = work / 'durable-recovery.json'
                aws('s3api', 'get-object', '--bucket', secured['bucket'], '--key', obj['Key'], '--version-id', obj['VersionId'], str(target))
                require(digest(target) == Path(obj['Key']).stem, 'durable journal digest differs')
                value = json.loads(target.read_text())
                validate_recovery(value, identity)
                return value
    # A crash can precede bucket privacy/encryption/versioning. KMS source tags are written atomically with key creation. Bucket tags
    # are verified independently: an interrupted CreateBucket before tagging
    # remains an unresolved ownership liability, never an inferred import.
    candidates = []
    for key in aws('kms', 'list-keys').get('Keys', []):
        tags = discover_customer_key_tags(key['KeyId'])
        if tags is None:
            continue
        if tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == cfg['name_prefix'] and tags.get('BootstrapExecution') == run_id:
            require(all(tag in tags for tag in IDENTITY_TAGS.values()), 'creation tags lack durable source/config identity; retain liability')
            candidates.append({key: tags[tag] for key, tag in IDENTITY_TAGS.items()})
    for kind in ['state', 'plans']:
        bucket = f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-{kind}-' + os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID']
        if exists('head-bucket', '--bucket', bucket):
            observed = optional_aws('s3api', 'get-bucket-tagging', '--bucket', bucket)
            tags = {tag['Key']: tag['Value'] for tag in (observed or {}).get('TagSet', [])}
            require(tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == cfg['name_prefix'] and tags.get('BootstrapExecution') == run_id, 'creation bucket ownership/producer differs; retain collision liability')
            require(all(tag in tags for tag in IDENTITY_TAGS.values()), 'creation bucket lacks durable source/config identity')
            candidates.append({key: tags[tag] for key, tag in IDENTITY_TAGS.items()})
    require(candidates and all(value == candidates[0] for value in candidates), 'cannot uniquely recover creation identity; retain partial custody liability')
    original = candidates[0]
    require(original['foundation_source_sha'] == producer['head_sha'] and recovery_compatible(original, identity), 'creation tags differ from reviewed recovery producer/config')
    value = journal(cfg, original, work, run_id=run_id)
    validate_recovery(value, identity)
    return value


def allowed_address(address):
    base = address.split('[', 1)[0]
    return base in CUSTODY and (address == base or address in (base + '["state"]', base + '["plans"]'))


def exists(*args):
    result = subprocess.run(['aws', 's3api', *args], capture_output=True)
    if result.returncode:
        require(b'404' in result.stderr or b'Not Found' in result.stderr or b'NoSuchKey' in result.stderr, 'cannot distinguish absent custody from authorization failure')
        return False
    return True


def optional_aws(*args):
    result = subprocess.run(['aws', *args, '--output', 'json'], capture_output=True)
    if result.returncode:
        absent = (b'NoSuchBucketPolicy', b'NoSuchPublicAccessBlockConfiguration', b'OwnershipControlsNotFoundError', b'NoSuchLifecycleConfiguration', b'NoSuchTagSet')
        if any(code in result.stderr for code in absent):
            return None
        raise ValueError('custody discovery API failed; authorization errors are not absence')
    return json.loads(result.stdout)


def secure_backend(binding):
    bucket = binding['bucket']
    prefix = binding['key'].split('/')[2]
    tags = {tag['Key']: tag['Value'] for tag in aws('s3api', 'get-bucket-tagging', '--bucket', bucket).get('TagSet', [])}
    require(tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == prefix, 'existing custody bucket ownership tags differ; retain liability and refuse adoption')
    rules = aws('s3api', 'get-bucket-encryption', '--bucket', bucket)['ServerSideEncryptionConfiguration']['Rules']
    default = rules[0]['ApplyServerSideEncryptionByDefault']
    require(default['SSEAlgorithm'] == 'aws:kms', 'bootstrap backend must use KMS encryption')
    account = os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID']
    require(re.fullmatch('arn:aws:kms:' + re.escape(binding['region']) + ':' + account + r':key/[a-f0-9-]+', default['KMSMasterKeyID']), 'custody KMS key is outside the reviewed account/region')
    key_tags = {tag['TagKey']: tag['TagValue'] for tag in aws('kms', 'list-resource-tags', '--key-id', default['KMSMasterKeyID']).get('Tags', [])}
    require(key_tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and key_tags.get('ResetBoundary') == prefix, 'custody KMS ownership tags differ')
    block = aws('s3api', 'get-public-access-block', '--bucket', bucket)['PublicAccessBlockConfiguration']
    require(all(block.get(key) is True for key in ('BlockPublicAcls', 'IgnorePublicAcls', 'BlockPublicPolicy', 'RestrictPublicBuckets')), 'bootstrap custody is not private')
    require(aws('s3api', 'get-bucket-versioning', '--bucket', bucket).get('Status') == 'Enabled', 'bootstrap custody must be versioned')
    return binding | {'kms_key_id': default['KMSMasterKeyID']}


def journal(cfg, identity, work, *, remote_state=None, run_id=None):
    run_id = run_id or os.environ['GITHUB_RUN_ID']
    expected_tags = creation_tags(identity, run_id)
    # Custody contains AWS-owned KMS material and metadata only. Preserve only
    # import IDs, never Terraform state, values, attributes, plans or logs.
    rows = []
    state_file = TF / 'terraform.tfstate'
    if state_file.exists():
        state = json.loads(state_file.read_text())
        for resource in state.get('resources', []):
            if resource.get('mode') != 'managed' or resource.get('module'):
                continue
            base = resource['type'] + '.' + resource['name']
            if base not in CUSTODY:
                continue
            for instance in resource.get('instances', []):
                address = base
                if 'index_key' in instance:
                    address += '[' + json.dumps(instance['index_key']) + ']'
                require(allowed_address(address), 'noncustody resource in initial recovery journal')
                resource_id = instance.get('attributes', {}).get('id')
                if resource_id:
                    rows.append({'address': address, 'id': resource_id})
    # AWS can acknowledge creation after cancellation but before Terraform has
    # flushed local state. Discover only owned, deterministic custody names and
    # the creation-tagged key; never infer application/identity resources.
    discovered = set(row['address'] for row in rows)
    discovery_complete = True
    unmapped_owned_keys = []
    unverified_buckets = []
    try:
        if 'aws_kms_key.custody' not in discovered:
            matches = []
            for key in aws('kms', 'list-keys').get('Keys', []):
                tags = discover_customer_key_tags(key['KeyId'])
                if tags is None:
                    continue
                if tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == cfg['name_prefix'] and tags.get('BootstrapExecution') == run_id:
                    require(all(tags.get(key) == val for key, val in expected_tags.items()), 'creation key identity tags differ')
                    matches.append(key['KeyId'])
            if len(matches) == 1:
                rows.append({'address': 'aws_kms_key.custody', 'id': matches[0]})
            elif len(matches) > 1:
                discovery_complete = False
                unmapped_owned_keys = matches
        owned = {bucket['Name'] for bucket in aws('s3api', 'list-buckets').get('Buckets', [])}
        account = os.environ.get('PILOT_BOOTSTRAP_ACCOUNT_ID', '')
        for kind in ['state', 'plans']:
            bucket = f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-{kind}-{account}'
            if bucket not in owned:
                continue
            observed = optional_aws('s3api', 'get-bucket-tagging', '--bucket', bucket)
            tags = {tag['Key']: tag['Value'] for tag in (observed or {}).get('TagSet', [])}
            if tags.get('ResetBoundary') != cfg['name_prefix'] or any(tags.get(key) != val for key, val in expected_tags.items()):
                unverified_buckets.append(bucket)
                discovery_complete = False
                continue
            address = 'aws_s3_bucket.custody[' + json.dumps(kind) + ']'
            if address not in discovered:
                rows.append({'address': address, 'id': bucket})
            controls = {
                'aws_s3_bucket_public_access_block.custody': 'get-public-access-block',
                'aws_s3_bucket_ownership_controls.custody': 'get-bucket-ownership-controls',
                'aws_s3_bucket_server_side_encryption_configuration.custody': 'get-bucket-encryption',
                'aws_s3_bucket_policy.custody': 'get-bucket-policy',
            }
            for base, getter in controls.items():
                address = base + '[' + json.dumps(kind) + ']'
                if address in discovered:
                    continue
                if optional_aws('s3api', getter, '--bucket', bucket) is not None:
                    rows.append({'address': address, 'id': bucket})
            versioning = aws('s3api', 'get-bucket-versioning', '--bucket', bucket)
            address = 'aws_s3_bucket_versioning.custody[' + json.dumps(kind) + ']'
            if versioning.get('Status') and address not in discovered:
                rows.append({'address': address, 'id': bucket})
            if kind == 'plans' and 'aws_s3_bucket_lifecycle_configuration.plans' not in discovered:
                if optional_aws('s3api', 'get-bucket-lifecycle-configuration', '--bucket', bucket) is not None:
                    rows.append({'address': 'aws_s3_bucket_lifecycle_configuration.plans', 'id': bucket})
    except (ValueError, KeyError, OSError):
        discovery_complete = False
    value = {'schema': 'honua.hosted-pilot-custody-recovery/v1', **identity,
             'bootstrap_run_id': run_id,
             'resources': rows, 'remote_recovery_state': remote_state, 'cloud_discovery_complete': discovery_complete, 'unmapped_owned_key_ids': unmapped_owned_keys, 'unverified_bucket_names': unverified_buckets,
             'cloud_discovery_tags': expected_tags | {'ResetBoundary': cfg['name_prefix']},
             'foundation_backend': {'bucket': f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-state-' + os.environ.get('PILOT_BOOTSTRAP_ACCOUNT_ID', ''), 'key': f'honua/hosted-pilot/{cfg["name_prefix"]}/foundation.tfstate'},
             'status': 'recovery-required-until-remote-state-verified'}
    private_json(work / 'custody-recovery.json', value)
    return value


def refuse_unbound_initial_custody(cfg, work):
    """No implicit fresh allowance after any partial creation, including key-only."""
    path = work / 'recovery-input/custody-recovery.json'
    recovery = json.loads(path.read_text()) if path.exists() else None
    expected = creation_tags({key: recovery[key] for key in IDENTITY_TAGS}, recovery['bootstrap_run_id']) if recovery else None
    owned_keys = []
    for key in aws('kms', 'list-keys').get('Keys', []):
        tags = discover_customer_key_tags(key['KeyId'])
        if tags is None:
            continue
        if tags.get('ManagedBy') == 'honua-release-hosted-pilot-bootstrap' and tags.get('ResetBoundary') == cfg['name_prefix']:
            require(expected is not None, 'existing custody key requires explicit original recovery_run_id; no new initial plan')
            require(all(tags.get(name) == value for name, value in expected.items()), 'existing custody key differs from original recovery binding; retain all creation liability')
            owned_keys.append(key['KeyId'])
    require(len(owned_keys) <= 1, 'multiple custody keys retain unresolved liability; no initial plan')
    for kind in ['state', 'plans']:
        bucket = f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-{kind}-' + os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID']
        if exists('head-bucket', '--bucket', bucket):
            require(expected is not None, 'existing custody bucket requires explicit original recovery_run_id; no new initial plan')
            observed = optional_aws('s3api', 'get-bucket-tagging', '--bucket', bucket)
            tags = {tag['Key']: tag['Value'] for tag in (observed or {}).get('TagSet', [])}
            require(tags.get('ResetBoundary') == cfg['name_prefix'] and all(tags.get(name) == value for name, value in expected.items()), 'existing custody bucket differs from original recovery binding; retain collision liability')


def bootstrap_contract_digest():
    # Exclude the compatibility map itself to avoid a self-referential commit
    # hash. The selected root SHA is independently bound to reviewed trunk.
    base = ROOT / 'certification/hosted-pilot/bootstrap'
    paths = sorted(path for path in base.glob('*.tf') if path.name != 'recovery-imports.tf')
    paths += [base / '.terraform.lock.hcl', base / 'foundation-bootstrap-workflow.yml', ROOT / 'tools/hosted_pilot/bootstrap.py', ROOT / 'tools/hosted_pilot/deploy.py', ROOT / 'tools/hosted_pilot/bootstrap_phase.py', ROOT / 'tools/hosted_pilot/custody_completion.py']
    value = hashlib.sha256()
    for path in paths:
        value.update(str(path.relative_to(ROOT)).encode() + b'\0' + path.read_bytes() + b'\0')
    return value.hexdigest()


def recovery_compatible(value, identity):
    if all(value.get(key) == val for key, val in identity.items()):
        return True
    policy = json.loads((ROOT / 'certification/hosted-pilot/bootstrap/recovery-compatibility.json').read_text())
    require(policy['schema'] == 'honua.hosted-pilot-recovery-compatibility/v1', 'unsupported recovery compatibility policy')
    for entry in policy['migrations']:
        if (entry['from_release_root_sha'] == value.get('release_root_sha') and
            entry['from_foundation_source_sha'] == value.get('foundation_source_sha') and
            entry['config_sha256'] == value.get('config_sha256') == identity['config_sha256'] and
            entry['from_provider_lock_sha256'] == value.get('provider_lock_sha256') and
            entry['to_provider_lock_sha256'] == identity['provider_lock_sha256'] and
            entry['to_bootstrap_contract_sha256'] == bootstrap_contract_digest() and
            value.get('config_path') == identity['config_path'] and
            isinstance(entry.get('reason'), str) and entry['reason'].strip()):
            return True
    return False


def recover_imports(identity, work, binding=None, cfg=None):
    path = work / 'recovery-input/custody-recovery.json'
    if path.exists():
        require(digest(path) == os.environ['PILOT_RECOVERY_SHA256'], 'custody recovery journal digest mismatch')
        value = json.loads(path.read_text())
        validate_recovery(value, identity)
    elif os.environ.get('PILOT_RECOVERY_RUN_ID'):
        value = retrieve_recovery(binding, cfg, identity, work)
        path.parent.mkdir(parents=True, exist_ok=True)
        private_json(path, value)
    else:
        return
    # Preserve original creation identity through import and subsequent reruns.
    os.environ['PILOT_CREATION_RUN_ID'] = value['bootstrap_run_id']
    os.environ['PILOT_CREATION_IDENTITY'] = json.dumps({key: value[key] for key in IDENTITY_TAGS})
    private_json(work / 'creation-identity.json', {'run_id': value['bootstrap_run_id'], 'identity': json.loads(os.environ['PILOT_CREATION_IDENTITY'])})
    imports = []
    for row in value['resources']:
        require(allowed_address(row['address']) and isinstance(row['id'], str), 'unapproved custody import')
        imports.append('import {\n  to = ' + row['address'] + '\n  id = ' + json.dumps(row['id']) + '\n}\n')
    (TF / 'recovery-imports.tf').write_text('\n'.join(imports))


def save_recovery(binding, cfg, identity, work):
    remote = None
    if (TF / 'terraform.tfstate').exists():
        try:
            secured = secure_backend(binding)
            key = f"honua/hosted-pilot/{cfg['name_prefix']}/foundation-recovery/{os.environ['GITHUB_RUN_ID']}/{time.time_ns()}.tfstate"
            result = aws('s3api', 'put-object', '--bucket', secured['bucket'], '--key', key,
                         '--body', str(TF / 'terraform.tfstate'), '--server-side-encryption', 'aws:kms',
                         '--ssekms-key-id', secured['kms_key_id'], '--if-none-match', '*')
            require(result.get('VersionId') not in (None, 'null'), 'recovery state version missing')
            remote = {'bucket': secured['bucket'], 'key': key, 'version_id': result['VersionId'], 'kms_key_id': secured['kms_key_id'], 'sha256': digest(TF / 'terraform.tfstate')}
        except (ValueError, KeyError, OSError):
            # Never upload raw state to GitHub, even when initial custody failed.
            # The metadata-only import journal and AWS creation tags remain the
            # recovery boundary. No application/identity bootstrap ran yet.
            pass
    creation_identity = json.loads(os.environ.get('PILOT_CREATION_IDENTITY', json.dumps(identity)))
    creation_run = os.environ.get('PILOT_CREATION_RUN_ID', os.environ['GITHUB_RUN_ID'])
    journal(cfg, creation_identity, work, remote_state=remote, run_id=creation_run)
    try:
        secured = secure_backend(binding)
        path = work / 'custody-recovery.json'
        key = recovery_prefix(cfg, creation_run) + 'journals/' + digest(path) + '.json'
        # Existing identical bytes are already durable. No overwrite or delete.
        if not exists('head-object', '--bucket', secured['bucket'], '--key', key):
            result = aws('s3api', 'put-object', '--bucket', secured['bucket'], '--key', key,
                         '--body', str(path), '--server-side-encryption', 'aws:kms',
                         '--ssekms-key-id', secured['kms_key_id'], '--if-none-match', '*')
            require(result.get('VersionId') not in (None, 'null'), 'durable recovery journal version missing')
    except (ValueError, KeyError, OSError):
        # Before secure custody, atomically created identity tags remain the
        # recovery source. The workflow retains honest partial-creation liability.
        pass
    return remote


def execution_identity(caller, identity, account, run_id, attempt):
    """Only STS public identity enters the receipt; never credential material."""
    arn,principal=caller.get('Arn'),caller.get('UserId')
    require(caller.get('Account')==account and isinstance(arn,str) and isinstance(principal,str) and
        re.fullmatch(r'arn:aws:(?:iam::'+re.escape(account)+r':user/[A-Za-z0-9+=,.@_/-]+|sts::'+re.escape(account)+r':assumed-role/[A-Za-z0-9+=,.@_/-]+)',arn) and
        re.fullmatch(r'(?:AIDA|AROA)[A-Z0-9]+(?::[A-Za-z0-9+=,.@_-]+)?',principal),
        'foundation requires an actual supported STS principal identity')
    return {'schema':'honua.hosted-pilot-bootstrap-execution-identity/v1',**identity,
        'run_id':run_id,'run_attempt':attempt,'account_id':account,'principal_arn':arn,'principal_id':principal,
        'run_unique_session':False,
        'attribution_limit':'static or pre-existing credentials are not a run-unique session; require exact planned resources and original creation tags'}


def planned_creation_resources(value):
    """Project only names and creation tags of the three audited creation APIs."""
    fields={'aws_iam_role':'name','aws_s3_bucket':'bucket','aws_sfn_state_machine':'name'}
    result=[]
    for row in value.get('resource_changes',[]):
        if row.get('type') not in fields or 'create' not in row.get('change',{}).get('actions',[]):continue
        after=row['change'].get('after') or {};name=after.get(fields[row['type']])
        require(isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9+=,.@_-]{1,128}',name),
            'audited creation requires an exact planned physical name')
        tags=after.get('tags_all') or after.get('tags') or {}
        selected={key:tags[key] for key in ['ManagedBy','BootstrapExecution',*IDENTITY_TAGS.values()] if key in tags}
        require(set(selected)=={'ManagedBy','BootstrapExecution',*IDENTITY_TAGS.values()} and
            selected==creation_tags({key:selected[tag] for key,tag in IDENTITY_TAGS.items()},selected['BootstrapExecution']),
            'audited creation lost its exact source and execution tags')
        result.append({'address':row['address'],'type':row['type'],'name':name,'creation_tags':selected})
    return result


def plan(cfg_path, work, *, custody_only=False, observation_only=False):
    path = work / ('custody.tfplan' if custody_only else 'saved.tfplan')
    cmd = ['terraform', 'plan', '-input=false', '-no-color', f'-var-file={cfg_path}',
           '-var=bootstrap_execution_id=' + os.environ.get('PILOT_CREATION_RUN_ID', os.environ['GITHUB_RUN_ID']),
           '-var=bootstrap_creation_identity=' + os.environ['PILOT_CREATION_IDENTITY'], f'-out={path}']
    if custody_only:
        cmd += ['-target=' + address for address in sorted(CUSTODY)]
    run(cmd, log=work / 'plan.log')
    expected_sha = digest(path)
    shown = json.loads(run(['terraform', 'show', '-json', str(path)]))
    changes = summarize(shown)
    if observation_only:
        require(custody_only and all(row['actions'] in (['read'], ['no-op']) for row in changes) and
            not any(row.get('change', {}).get('importing') for row in shown.get('resource_changes', [])),
            'canonical custody reconciliation cannot create, import, update or destroy infrastructure')
    scope_path=work/'creation-resources.json'
    scope=json.loads(scope_path.read_text()) if scope_path.exists() else []
    for resource in planned_creation_resources(shown):
        require(not any(row['address']==resource['address'] and row!=resource for row in scope),'planned creation identity changed within one bootstrap')
        if resource not in scope:scope.append(resource)
    private_json(scope_path,scope)
    if custody_only:
        require(all(allowed_address(row['address']) or (row['address'] == 'data.aws_caller_identity.current' and row['actions'] in (['read'], ['no-op'])) for row in changes), 'initial custody plan attempts noncustody infrastructure')
    return path, changes, expected_sha


def exact_apply(path, expected, work):
    # The source-reviewed bootstrap applies only the locally produced binary.
    # It never accepts or uploads a binary plan supplied through workflow input.
    require(digest(path) == expected, 'bootstrap saved plan changed')
    run(['terraform', 'apply', '-input=false', '-no-color', str(path)], log=work / 'apply.log')
    return expected


def observe_original_custody(binding, identity, cfg, source_root, work, *, permit_unfinished=False):
    """Preserve actual original custody tags, separately from this phase's new resources."""
    from bootstrap_phase import api
    secured = secure_backend(binding)
    plans = dict(binding, bucket=f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-plans-{cfg["account_id"]}')
    secured_plans = secure_backend(plans)
    require(secured['kms_key_id'] == secured_plans['kms_key_id'], 'custody buckets no longer share their original key')
    observed = []
    for bucket in (secured['bucket'], secured_plans['bucket']):
        observed.append({row['Key']: row['Value'] for row in aws('s3api', 'get-bucket-tagging', '--bucket', bucket)['TagSet']})
    observed.append({row['TagKey']: row['TagValue'] for row in aws('kms', 'list-resource-tags', '--key-id', secured['kms_key_id'])['Tags']})
    keys = {'ManagedBy', 'BootstrapExecution', *IDENTITY_TAGS.values()}
    selected = [{key: tags[key] for key in keys if key in tags} for tags in observed]
    require(all(tags == selected[0] for tags in selected) and set(selected[0]) == keys, 'original custody creation identity is missing or inconsistent')
    tags = selected[0]
    original = {key: tags[name] for key, name in IDENTITY_TAGS.items()}
    require(tags == creation_tags(original, tags['BootstrapExecution']), 'custody source tags are invalid')
    require(re.fullmatch(r'[0-9]+', tags['BootstrapExecution']), 'original custody run is not observed')
    for key in ('foundation_source_sha', 'release_root_sha'):
        require(re.fullmatch(r'[a-f0-9]{40}', original[key]), 'original custody source is invalid')
    for key in ('config_sha256', 'provider_lock_sha256'):
        require(re.fullmatch(r'[a-f0-9]{64}', original[key]), 'original custody source digest is invalid')
    require(re.fullmatch(r'certification/hosted-pilot/bootstrap/deployments/[A-Za-z0-9._-]+\.tfvars\.json', original['config_path']), 'custody original configuration path is invalid')
    run(['git', '-C', str(source_root), 'merge-base', '--is-ancestor', original['foundation_source_sha'], identity['foundation_source_sha']])
    run(['git', '-C', str(ROOT), 'merge-base', '--is-ancestor', original['release_root_sha'], identity['release_root_sha']])
    original_config = run(['git', '-C', str(source_root), 'show', original['foundation_source_sha']+':'+original['config_path']])
    require(hashlib.sha256(original_config).hexdigest() == original['config_sha256'], 'custody original configuration source differs')
    original_value = json.loads(original_config)
    require(all(original_value.get(field) == cfg[field] for field in ('account_id', 'region', 'name_prefix')), 'custody original account, region or namespace differs')
    require(hashlib.sha256(run(['git', '-C', str(ROOT), 'show', original['release_root_sha']+':certification/hosted-pilot/bootstrap/.terraform.lock.hcl'])).hexdigest() == original['provider_lock_sha256'], 'custody original provider lock differs')
    producer = api('repos/honua-io/honua-iac/actions/runs/'+tags['BootstrapExecution'])
    workflow = api('repos/honua-io/honua-iac/actions/workflows/hosted-pilot-bootstrap.yml')
    require(producer.get('id') == int(tags['BootstrapExecution']) and producer.get('workflow_id') == workflow.get('id') and
            workflow.get('path') == '.github/workflows/hosted-pilot-bootstrap.yml' and producer.get('head_sha') == original['foundation_source_sha'] and
            producer.get('head_branch') == 'trunk' and producer.get('event') == 'workflow_dispatch' and producer.get('status') == 'completed' and producer.get('conclusion') in ('success', 'failure', 'cancelled', 'timed_out'),
            'custody original native producer differs or is still incomplete')
    creator = {'run_id': tags['BootstrapExecution'], 'identity': original}
    if producer['conclusion'] != 'success' and not permit_unfinished:
        from custody_completion import observe_successful_recovery
        observe_successful_recovery(cfg, secured, creator, work, aws=aws)
    private_json(work/'creation-identity.json', {'run_id': tags['BootstrapExecution'], 'identity': original})
    return tags


def phase_review(identity, actor, cfg, cfg_digest, phase, changes, plan_path, work):
    from bootstrap_phase import SCHEMA, deadlines
    review = {'schema': SCHEMA, **identity, 'resolved_config_sha256': cfg_digest,
        'execution_identity': actor, 'creation_resources': json.loads((work/'creation-resources.json').read_text()),
        'phase': phase, 'resource_changes': changes, 'plan_sha256': digest(plan_path),
        'run_id': os.environ['GITHUB_RUN_ID'], 'run_attempt': os.environ['GITHUB_RUN_ATTEMPT'], **deadlines(cfg),
        'custody_observation_only': (work/'custody-observation-only.json').exists(),
        'original_creation': {'run_id': os.environ.get('PILOT_CREATION_RUN_ID', os.environ['GITHUB_RUN_ID']),
            'identity': json.loads(os.environ.get('PILOT_CREATION_IDENTITY', json.dumps(identity)))},
        'custody_recovery': json.loads((work/'original-successful-custody.json').read_text()) if (work/'original-successful-custody.json').exists() else None}
    private_json(work/'review.json', review)
    return review


def prepare_phase(identity, actor, cfg, cfg_digest, phase, changes, plan_path, binding, remote, work):
    review = phase_review(identity, actor, cfg, cfg_digest, phase, changes, plan_path, work)
    head = aws('s3api', 'head-object', '--bucket', binding['bucket'], '--key', binding['key']) if remote and exists('head-object', '--bucket', binding['bucket'], '--key', binding['key']) else None
    if head is not None:
        require(head.get('VersionId') not in (None, 'null'), 'canonical foundation state is not versioned')
    private_json(work/'prepared.json', {'schema': 'honua.private-bootstrap-prepared/v1', 'review_sha256': digest(work/'review.json'),
        'review': review, 'binary': plan_path.name, 'binding': binding, 'remote': remote,
        'state_version_id': None if head is None else head['VersionId']})
    print('One exact phase prepared; saved binary remains only on this executor pending actual human review.')


def apply_prepared(identity, actor, cfg, cfg_digest, phase, binding, work):
    from bootstrap_phase import approved_review, stamp, validate_review, utcnow
    prepared = json.loads((work/'prepared.json').read_text())
    review = json.loads((work/'review.json').read_text())
    validate_review(review, phase=phase)
    require(prepared.get('schema') == 'honua.private-bootstrap-prepared/v1' and prepared.get('review') == review and
        prepared.get('review_sha256') == digest(work/'review.json') and prepared.get('binding') == binding and
        all(review.get(key) == value for key, value in identity.items()) and review.get('execution_identity') == actor and
        review.get('resolved_config_sha256') == cfg_digest, 'private saved-plan source, actor or configuration changed')
    require(stamp(review['execution_expires_at']) <= min(stamp(cfg['expires_at']), stamp(cfg['budget_ledger']['expires_at'])), 'saved phase exceeds original reservation expiry')
    expected_name = 'custody.tfplan' if phase == 'custody-only' else 'saved.tfplan'
    require(prepared.get('binary') == expected_name and (phase == 'custody-only' or prepared.get('remote') is True), 'saved phase binary or canonical backend differs')
    proof = approved_review(review, phase=phase)
    require(proof is not None, 'actual same-run exact-plan approval is absent')
    # A changed canonical state cannot be silently replanned after approval.
    if prepared['remote']:
        present = exists('head-object', '--bucket', binding['bucket'], '--key', binding['key'])
        if prepared['state_version_id'] is None:
            require(not present, 'canonical state appeared after review; new plan required')
        else:
            require(present, 'canonical state disappeared after review; new plan required')
            head = aws('s3api', 'head-object', '--bucket', binding['bucket'], '--key', binding['key'])
            require(head.get('VersionId') == prepared['state_version_id'], 'canonical state changed after review; new plan required')
    validate_review(review, phase=phase)
    private_json(work/'verified-approval.json', proof)
    path = work/expected_name
    require(digest(path) == review['plan_sha256'], 'exact approved binary plan changed')
    private_json(work/'apply-admitted.json', {'schema': 'honua.hosted-pilot-phase-admitted/v1', 'review': review, 'approval': proof,
        'admitted_at': utcnow().isoformat(), 'financial_settlement': False, 'partial_failure_liability_retained': True})
    exact_apply(path, review['plan_sha256'], work)
    if phase == 'custody-only':
        secured = secure_backend(binding)
        if not prepared['remote']:
            require(save_recovery(binding, cfg, identity, work), 'custody created but encrypted recovery state persistence failed')
            private_json(TF/'backend.tf.json', {'terraform': {'backend': {'s3': secured}}})
            run(['terraform', 'init', '-migrate-state', '-force-copy', '-input=false', '-no-color'], log=work/'migrate.log')
        require(exists('head-object', '--bucket', secured['bucket'], '--key', secured['key']), 'custody canonical state migration not verified')
        if prepared['remote']:
            save_recovery(binding, cfg, identity, work)
        # This receipt contains no application bindings; the foundation has not run.
        creator = review['original_creation']
        private_json(work/'custody-applied.json', {'schema': 'honua.hosted-pilot-custody-applied/v1', 'review': review,
            'approval': proof, 'original_creation': creator, 'state_binding': secured, 'apply_admission_sha256': digest(work/'apply-admitted.json'), 'custody_recovery_sha256': digest(work/'custody-recovery.json')})
        from custody_completion import retain
        retain(json.loads((work/'custody-applied.json').read_text()), cfg, secured, work, aws=aws, exists=exists)
        print('Only reviewed custody applied and canonical state verified; foundation phase has not run.')
        return
    outputs = json.loads(run(['terraform', 'output', '-json']))
    private_json(work/'bindings.json', {'schema': 'honua.hosted-pilot-bootstrap-bindings/v2',
        'phase_review': review, 'phase_approval': proof, 'apply_admission_sha256': digest(work/'apply-admitted.json'),
        'release_root_sha': os.environ['PILOT_RELEASE_ROOT_SHA'], 'foundation_source_sha': os.environ['GITHUB_SHA'],
        'foundation_run_id': os.environ['GITHUB_RUN_ID'], 'foundation_run_attempt': os.environ['GITHUB_RUN_ATTEMPT'],
        'config_path': identity['config_path'], 'config_sha256': identity['config_sha256'], 'resolved_config_sha256': cfg_digest,
        'execution_identity': actor, 'creation_resources': review['creation_resources'], 'saved_plan_sha256': review['plan_sha256'],
        **{key: outputs[key]['value'] for key in ['deployment_bindings', 'permissions_boundary_arn', 'budget_binding']}})
    print('Only reviewed remote-backed foundation applied; no target or campaign was dispatched.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['plan', 'prepare', 'apply', 'journal'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--phase', choices=['custody-only', 'remote-backed-foundation'])
    args = parser.parse_args()
    require(args.mode in ('plan', 'journal') or args.phase is not None, 'explicit single phase required')
    e = os.environ
    require(e['GITHUB_REPOSITORY'] == 'honua-io/honua-iac' and e['GITHUB_REF'] == 'refs/heads/trunk', 'foundation runs only in honua-iac trunk')
    require(re.fullmatch(r'[a-f0-9]{40}', e['PILOT_RELEASE_ROOT_SHA']), 'exact reviewed release root SHA required')
    require(run(['git', 'rev-parse', 'HEAD']).decode().strip() == e['PILOT_RELEASE_ROOT_SHA'], 'checked-out release root differs')
    source_root = Path(e['PILOT_FOUNDATION_SOURCE_ROOT']).resolve()
    cfg_path = (source_root / args.config).resolve()
    require(cfg_path.is_relative_to(source_root / 'certification/hosted-pilot/bootstrap/deployments') and cfg_path.name.endswith('.tfvars.json'), 'reviewed foundation config required')
    run(['git', '-C', str(source_root), 'ls-files', '--error-unmatch', str(cfg_path)])
    require(run(['git', '-C', str(source_root), 'rev-parse', 'HEAD']).decode().strip() == e['GITHUB_SHA'], 'foundation config source mismatch')
    require(not any(key.startswith(('TF_VAR_', 'TF_CLI_ARGS')) for key in e), 'ambient Terraform configuration refused')
    os.umask(0o077)
    work = Path(e['RUNNER_TEMP']) / 'hosted-pilot-bootstrap-private'
    work.mkdir(mode=0o700, exist_ok=True)
    cfg = json.loads(cfg_path.read_text())
    cfg = resolve_image_funding(cfg, ROOT, e['PILOT_RELEASE_ROOT_SHA'], config_path=cfg_path.relative_to(source_root).as_posix())
    require(not cfg.get('bootstrap_custody_creation_tags'), 'caller-provided custody creation identity refused')
    require(cfg['region'] == e['AWS_REGION'], 'bootstrap region mismatch')
    require(re.fullmatch(r'honua-pilot-[a-z0-9-]{4,16}', cfg['name_prefix']), 'invalid isolated prefix')
    caller = aws('sts', 'get-caller-identity')
    account = caller['Account']
    require(cfg['account_id'] == account, 'foundation AWS account differs from reviewed configuration')
    os.environ['PILOT_BOOTSTRAP_ACCOUNT_ID'] = account
    binding = {'bucket': f'honua-release-{cfg["name_prefix"].removeprefix("honua-")}-foundation-state-{account}',
               'key': f'honua/hosted-pilot/{cfg["name_prefix"]}/foundation.tfstate', 'region': cfg['region'], 'encrypt': True, 'use_lockfile': True}
    identity = {'foundation_source_sha': e['GITHUB_SHA'], 'release_root_sha': e['PILOT_RELEASE_ROOT_SHA'],
                'config_path': args.config, 'config_sha256': digest(cfg_path),
                'provider_lock_sha256': digest(TF / '.terraform.lock.hcl')}
    actor = execution_identity(caller,identity,account,e['GITHUB_RUN_ID'],e['GITHUB_RUN_ATTEMPT'])
    private_json(work/'execution-identity.json',actor)
    resolved_cfg_path = work / 'foundation-resolved.tfvars.json'
    private_json(resolved_cfg_path, cfg)
    resolved_cfg_digest = digest(resolved_cfg_path)
    if (work / 'creation-identity.json').exists():
        creation = json.loads((work / 'creation-identity.json').read_text())
        os.environ['PILOT_CREATION_RUN_ID'] = creation['run_id']
        os.environ['PILOT_CREATION_IDENTITY'] = json.dumps(creation['identity'])
    else:
        os.environ['PILOT_CREATION_IDENTITY'] = json.dumps(identity)
    if args.mode == 'journal':
        # Also runs with always() after failed/cancelled initial apply.
        save_recovery(binding, cfg, identity, work)
        with open(e['GITHUB_STEP_SUMMARY'], 'a') as summary:
            summary.write('Custody recovery metadata SHA256: `' + digest(work / 'custody-recovery.json') + '`\n')
        return
    if args.mode == 'apply':
        # Reload only the same private resolved configuration prepared in this
        # job. Do not init, import, plan or fall back to an alternative phase.
        # The source compiler above must match every field except the custody
        # tags independently observed during preparation.
        original_cfg = json.loads((work/'prepared-config.json').read_text())
        require({key: value for key, value in original_cfg.items() if key != 'bootstrap_custody_creation_tags'} == cfg, 'source resolved configuration changed')
        private_json(resolved_cfg_path, original_cfg)
        apply_prepared(identity, actor, original_cfg, digest(resolved_cfg_path), args.phase, binding, work)
        return
    recover_imports(identity, work, binding, cfg)
    remote = False
    has_state = False
    if exists('head-bucket', '--bucket', binding['bucket']):
        try:
            secured = secure_backend(binding)
            private_json(TF / 'backend.tf.json', {'terraform': {'backend': {'s3': secured}}})
            run(['terraform', 'init', '-lockfile=readonly', '-input=false', '-no-color'], log=work / 'init.log')
            remote = True
            has_state = exists('head-object', '--bucket', secured['bucket'], '--key', secured['key'])
            require(has_state or (work / 'recovery-input/custody-recovery.json').exists(), 'custody exists without canonical state; reviewed recovery journal required before adopting resources')
        except (ValueError, KeyError):
            require((work / 'recovery-input/custody-recovery.json').exists(), 'partial insecure custody requires its governed recovery journal; no application infrastructure will run')
    if args.phase == 'remote-backed-foundation':
        require(remote and has_state, 'foundation phase requires existing secured canonical custody; custody apply is a separate reviewed run')
    custody_observation = args.phase == 'custody-only' and remote and has_state
    if custody_observation:
        private_json(work/'custody-observation-only.json', {'schema': 'honua.custody-observation-only/v1', 'infrastructure_mutation_allowed': False})
    if remote and has_state:
        cfg['bootstrap_custody_creation_tags'] = observe_original_custody(binding, identity, cfg, source_root, work, permit_unfinished=custody_observation)
        # Only custody retains original producer tags; this phase's newly created
        # IAM/orchestration resources belong to the current native run.
        if custody_observation:
            e['PILOT_CREATION_RUN_ID'] = cfg['bootstrap_custody_creation_tags']['BootstrapExecution']
            e['PILOT_CREATION_IDENTITY'] = json.dumps({key: cfg['bootstrap_custody_creation_tags'][tag] for key, tag in IDENTITY_TAGS.items()})
        else:
            e['PILOT_CREATION_RUN_ID'] = e['GITHUB_RUN_ID']
            e['PILOT_CREATION_IDENTITY'] = json.dumps(identity)
        private_json(resolved_cfg_path, cfg)
        resolved_cfg_digest = digest(resolved_cfg_path)
    private_json(work/'prepared-config.json', cfg)
    if not remote or not has_state or custody_observation:
        refuse_unbound_initial_custody(cfg, work)
        if not remote:
            run(['terraform', 'init', '-backend=false', '-lockfile=readonly', '-input=false', '-no-color'], log=work / 'init.log')
        initial, changes, initial_sha = plan(resolved_cfg_path, work, custody_only=True, observation_only=True) if custody_observation else plan(resolved_cfg_path, work, custody_only=True)
        if custody_observation:
            require(all(row['actions'] in (['read'], ['no-op']) for row in changes), 'canonical custody cannot be maintained or replaced by reconciliation')
        if args.mode == 'prepare':
            prepare_phase(identity, actor, cfg, resolved_cfg_digest, 'custody-only', changes, initial, binding, remote, work)
        else:
            phase_review(identity, actor, cfg, resolved_cfg_digest, 'custody-only', changes, initial, work)
            print('Custody-only plan metadata prepared; no binary plan/state/log is uploaded.')
        return
    full, changes, full_sha = plan(resolved_cfg_path, work)
    require(all(not allowed_address(row['address']) or row['actions'] in (['read'], ['no-op']) for row in changes), 'foundation phase attempts custody maintenance; original custody must remain unchanged')
    if args.mode == 'prepare':
        prepare_phase(identity, actor, cfg, resolved_cfg_digest, 'remote-backed-foundation', changes, full, binding, True, work)
    else:
        phase_review(identity, actor, cfg, resolved_cfg_digest, 'remote-backed-foundation', changes, full, work)
        print('Remote-backed foundation plan metadata prepared; no infrastructure applied.')


if __name__ == '__main__':
    try:
        main()
    except (KeyError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f'bootstrap refused: {error}', file=sys.stderr)
        sys.exit(1)
