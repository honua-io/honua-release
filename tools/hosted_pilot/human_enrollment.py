#!/usr/bin/env python3
"""Seal private human enrollment input for the existing reviewed Terraform workflow.

This helper only describes existing AWS resources and encrypts input. Cognito
users and memberships are created by the saved-plan deployment, never here.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import secrets
import stat
import string
import sys

SCHEMA = 'honua.pilot-human-enrollment/v1'
ALIAS = re.compile(r'[a-z][a-z0-9_]{2,31}')
PREFIX = re.compile(r'honua-pilot-[a-z0-9-]{4,16}')
KEY = re.compile(r'arn:aws:kms:([a-z0-9-]+):(\d{12}):key/([a-f0-9-]{36})')
EMAIL = re.compile(r'[^\s@]{1,64}@[^\s@]+\.[^\s@]+')
ROLES = {'authors', 'approvers'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def private_input(path):
    path = Path(path)
    require(not path.is_symlink(), 'private input must not be a symbolic link')
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and not info.st_mode & 0o077,
            'private input requires a regular owner-only file')
    require(info.st_size <= 16384, 'private enrollment input is too large')
    return json.loads(path.read_text())


def validate_people(value):
    require(isinstance(value, dict) and 2 <= len(value) <= 10,
            'enroll between two and ten explicitly nominated people')
    people = {}
    for alias, person in value.items():
        require(isinstance(alias, str) and ALIAS.fullmatch(alias), 'use opaque enrollment aliases')
        require(isinstance(person, dict) and set(person) == {'email', 'role', 'emailOwnershipConfirmed'},
                'private person record requires email, role and emailOwnershipConfirmed')
        email = person['email']
        require(isinstance(email, str) and len(email) <= 128 and EMAIL.fullmatch(email) and
                email == email.strip() and person['emailOwnershipConfirmed'] is True,
                'the nominating operator must confirm each exact human email')
        require(isinstance(person['role'], str) and person['role'] in ROLES,
                'each person has one author or approver role')
        people[alias] = person | {'email': email.lower()}
    require(len({p['email'] for p in people.values()}) == len(people),
            'distinct aliases must identify distinct human emails')
    require({p['role'] for p in people.values()} == ROLES,
            'a separate author and approver are required')
    return people


def encryption_context(prefix, pool_id, alias, role):
    return {'Purpose': SCHEMA, 'Pilot': prefix, 'PoolId': pool_id, 'Alias': alias, 'Role': role}


def validate_configuration(value, *, region, custody_key_arn):
    """Validate only ciphertext/reference fields before any deployment action."""
    if value is None:
        return
    require(isinstance(value, dict) and set(value) == {'pool_id', 'key_arn', 'users'},
            'enrollment configuration contains unreviewed fields')
    require(isinstance(value['pool_id'], str) and re.fullmatch(re.escape(region) + r'_[A-Za-z0-9]+', value['pool_id']),
            'enrollment pool must be in the reviewed region')
    require(value['key_arn'] == custody_key_arn and KEY.fullmatch(custody_key_arn),
            'enrollment must use the original private state custody key')
    users = value['users']
    require(isinstance(users, dict) and 2 <= len(users) <= 10, 'bounded distinct human enrollment required')
    roles = set()
    for alias, person in users.items():
        require(isinstance(alias, str) and ALIAS.fullmatch(alias), 'opaque enrollment alias required')
        require(isinstance(person, dict) and set(person) == {'role', 'payload'},
                'only role and encrypted payload may enter source')
        require(isinstance(person['role'], str) and person['role'] in ROLES, 'unknown enrollment role')
        roles.add(person['role'])
        payload = person['payload']
        require(isinstance(payload, str) and 44 <= len(payload) <= 8192, 'bounded encrypted payload required')
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            raise ValueError('invalid encrypted enrollment payload') from None
        require(32 <= len(decoded) <= 6144, 'invalid encrypted enrollment size')
    require(roles == ROLES, 'separate author and approver enrollment required')


def temporary_password():
    groups = (string.ascii_lowercase, string.ascii_uppercase, string.digits, '!@#%+-_')
    chars = [secrets.choice(group) for group in groups]
    chars.extend(secrets.choice(''.join(groups)) for _ in range(28))
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


def seal(people, *, prefix, pool_id, key_arn, sts, cognito, kms):
    people = validate_people(people)
    key = KEY.fullmatch(key_arn)
    require(PREFIX.fullmatch(prefix) and key is not None, 'explicit pilot prefix and KMS key ARN required')
    region, account = key.group(1), key.group(2)
    require(re.fullmatch(re.escape(region) + r'_[A-Za-z0-9]+', pool_id), 'pool and key regions must match')
    require(sts.get_caller_identity()['Account'] == account, 'AWS account does not match the reviewed key')
    pool = cognito.describe_user_pool(UserPoolId=pool_id)['UserPool']
    require(pool['Id'] == pool_id and pool['Name'] == prefix + '-operators' and
            pool['Arn'] == f'arn:aws:cognito-idp:{region}:{account}:userpool/{pool_id}' and
            pool.get('AdminCreateUserConfig', {}).get('AllowAdminCreateUserOnly') is True,
            'actual existing pool does not match the isolated pilot')
    tags = cognito.list_tags_for_resource(ResourceArn=pool['Arn'])['Tags']
    require(tags.get('ResetBoundary') == prefix and tags.get('ManagedBy') == 'honua-release-hosted-pilot',
            'actual pool ownership tags do not match the reviewed pilot')
    metadata = kms.describe_key(KeyId=key_arn)['KeyMetadata']
    require(metadata['Arn'] == key_arn and metadata['KeyState'] == 'Enabled' and
            metadata['KeyUsage'] == 'ENCRYPT_DECRYPT' and metadata['KeySpec'] == 'SYMMETRIC_DEFAULT',
            'an enabled symmetric custody key is required')
    encrypted = {'pool_id': pool_id, 'key_arn': key_arn, 'users': {}}
    private = {'schema': SCHEMA, 'poolId': pool_id, 'pilot': prefix, 'people': {},
               'accountsCreated': False, 'invitationsSent': False}
    for alias, person in people.items():
        password = temporary_password()
        plaintext = {'schema': SCHEMA, 'email': person['email'], 'temporaryPassword': password,
                     'emailOwnershipConfirmed': True}
        result = kms.encrypt(KeyId=key_arn, EncryptionAlgorithm='SYMMETRIC_DEFAULT',
                             EncryptionContext=encryption_context(prefix, pool_id, alias, person['role']),
                             Plaintext=json.dumps(plaintext, sort_keys=True).encode())
        require(result['KeyId'] == key_arn and isinstance(result['CiphertextBlob'], bytes) and
                32 <= len(result['CiphertextBlob']) <= 6144, 'KMS returned an unexpected encryption binding')
        encrypted['users'][alias] = {'role': person['role'],
                                    'payload': base64.b64encode(result['CiphertextBlob']).decode('ascii')}
        private['people'][alias] = {'email': person['email'], 'role': person['role'], 'temporaryPassword': password}
    return encrypted, private


def write_new_private(path, value):
    # Never overwrite a prior handoff or follow a final-component symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-input', required=True)
    parser.add_argument('--name-prefix', required=True)
    parser.add_argument('--pool-id', required=True)
    parser.add_argument('--key-arn', required=True)
    parser.add_argument('--sealed-output', required=True)
    parser.add_argument('--private-handoff', required=True)
    args = parser.parse_args()
    require(Path(args.sealed_output).resolve() != Path(args.private_handoff).resolve(),
            'sealed configuration and private handoff need different destinations')
    require(not Path(args.sealed_output).exists() and not Path(args.private_handoff).exists(),
            'output already exists; preserve prior enrollment and handoff')
    people = private_input(args.private_input)
    validate_people(people)
    key = KEY.fullmatch(args.key_arn)
    require(key is not None, 'explicit custody key ARN required')
    import boto3
    session = boto3.Session(region_name=key.group(1))
    sealed, handoff = seal(people, prefix=args.name_prefix, pool_id=args.pool_id, key_arn=args.key_arn,
                          sts=session.client('sts'), cognito=session.client('cognito-idp'), kms=session.client('kms'))
    write_new_private(args.private_handoff, handoff)
    write_new_private(args.sealed_output, sealed)
    print('Sealed enrollment and private handoff written. No users, memberships or invitations were created.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Provider/JSON errors can contain private input. Do not echo them.
        print(f'human enrollment refused ({type(error).__name__}); no account provisioning was performed', file=sys.stderr)
        sys.exit(1)
