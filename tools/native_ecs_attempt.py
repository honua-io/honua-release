"""Durable ownership for one native ECS qualification stack attempt."""
import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ecs_honua_recovery import atomic
from launch_stack_observations import one, require

STACK_ARN = re.compile(
    r'arn:aws:cloudformation:([a-z0-9-]+):([0-9]{12}):stack/'
    r'(honua-gate-[A-Za-z0-9-]+)/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
)
READ_ATTEMPTS = 5


class AmbiguousStackCreate(Exception):
    """Adapter signal that CreateStack may have succeeded without acknowledgement."""


class RetryableStackRead(Exception):
    """Adapter signal for a transient provider error or initial propagation delay."""


@dataclass(frozen=True)
class StackAttempt:
    journal: Path
    stack_name: str
    account: str
    region: str
    attempt_id: str

    def scope(self, image):
        require(re.fullmatch(r'honua-gate-[A-Za-z0-9-]+', self.stack_name)
                and len(self.stack_name) <= 128, 'Expected a disposable stack name')
        require(re.fullmatch(r'[0-9]{12}', self.account)
                and re.fullmatch(r'[a-z0-9-]+', self.region), 'Invalid stack account or region')
        require(re.fullmatch(r'[A-Za-z0-9-]{1,128}', self.attempt_id), 'Invalid stack attempt ID')
        return {'stackName': self.stack_name, 'account': self.account, 'region': self.region,
                'attemptId': self.attempt_id, 'image': image}


@dataclass(frozen=True)
class OwnedAttempt:
    scope: StackAttempt
    token: str
    replay: bool

    @property
    def stack_name(self):
        return self.scope.stack_name

    @property
    def tags(self):
        return [
            {'Key': 'HonuaGateAttempt', 'Value': self.scope.attempt_id},
            {'Key': 'HonuaGateOwner', 'Value': self.token},
        ]

    @property
    def arn_path(self):
        return self.scope.journal.with_suffix(self.scope.journal.suffix + '.arn')

    def check_arn(self, stack_id):
        match = STACK_ARN.fullmatch(stack_id) if isinstance(stack_id, str) else None
        require(match and match.groups() == (self.scope.region, self.scope.account, self.stack_name),
                'Stack ARN differs from the admitted account, region or name')

    def retained_arn(self):
        if not self.arn_path.exists():
            return None
        stack_id = json.loads(self.arn_path.read_text())['stackId']
        self.check_arn(stack_id)
        return stack_id

    def pin_arn(self, stack_id):
        self.check_arn(stack_id)
        retained = self.retained_arn()
        if retained is None:
            persist(self.arn_path, {'stackId': stack_id})
        else:
            require(retained == stack_id, 'Stack ARN differs from the retained immutable identity')

    def observe(self, aws, *, stack_id=None):
        response = describe_with_retry(aws, stack_id or self.stack_name)
        stack = one(response.get('Stacks'), 'Missing or ambiguous stack')
        observed_id = stack.get('StackId')
        self.check_arn(observed_id)
        require(stack.get('StackName') == self.stack_name and (stack_id is None or observed_id == stack_id),
                'Stack identity differs from the requested stack')
        expected = [*self.tags, {'Key': 'HonuaGate', 'Value': 'true'}]
        tags = stack.get('Tags', [])
        for tag in expected:
            require([item for item in tags if item.get('Key') == tag['Key']] == [tag],
                    'Could not confirm gate stack ownership')
        self.pin_arn(observed_id)
        return stack


def describe_with_retry(aws, identity):
    for attempt in range(1, READ_ATTEMPTS + 1):
        try:
            return aws('cloudformation', 'describe-stacks', '--stack-name', identity)
        except (RetryableStackRead, TimeoutError, ConnectionError):
            if attempt == READ_ATTEMPTS:
                raise
            time.sleep(1)


def persist(path, record):
    atomic(path, record)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def open_attempt(scope, image):
    expected = scope.scope(image)
    journal = scope.journal
    journal.parent.mkdir(parents=True, exist_ok=True)
    with journal.with_suffix(journal.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        replay = journal.exists()
        if replay:
            record = json.loads(journal.read_text())
            token = record.get('ownershipToken')
            require(isinstance(token, str) and re.fullmatch(r'[0-9a-f]{32}', token)
                    and record == {**expected, 'ownershipToken': token},
                    'Durable stack attempt identity differs')
        else:
            token = uuid4().hex
            persist(journal, {**expected, 'ownershipToken': token})
        yield OwnedAttempt(scope, token, replay)
