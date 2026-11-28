"""AWS CLI adapter and live Hardened/Redis-off checks for Launch Stack qualification."""
from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any, Callable
from urllib.parse import urlsplit

from launch_stack_observations import one, require, resource_summaries, server_container
from native_ecs_attempt import AmbiguousStackCreate, OwnedAttempt, RetryableStackRead

AWS_CMD_TIMEOUT_SECONDS = 3600
RDS_UNPROTECT_ATTEMPTS = 12
RDS_UNPROTECT_POLL_SECONDS = 15
REQUIRED_PROTECTION = 'Contain'
REQUIRED_WATCHDOG_MODE = 'NotifyOnly'
ELASTICACHE_PREFIX = 'AWS::ElastiCache::'
REDIS_TOKEN = 'redis'
COMPLETE_STATUSES = frozenset({'CREATE_COMPLETE', 'UPDATE_COMPLETE'})
PROTECTED_STAGES = frozenset({'Contain', 'Strict'})
AWS_ERROR_CODE_RE = re.compile(r'An error occurred \(([^)]+)\)')
ACCOUNT_ID_RE = re.compile(r'^[0-9]{12}$')
RETRYABLE_AWS_CODES = frozenset({'Throttling', 'ThrottlingException', 'RequestLimitExceeded'})
NOT_FOUND_AWS_CODES = frozenset({'DBInstanceNotFound', 'LoadBalancerNotFound'})


class AwsCommandError(RuntimeError):
    """Sanitized AWS CLI failure carrying only service, operation, and error code."""

    def __init__(self, message: str, error_code: str = '') -> None:
        super().__init__(message)
        self.error_code = error_code


def parse_aws_error_code(stderr: str) -> str:
    match = AWS_ERROR_CODE_RE.search(stderr)
    return match.group(1) if match else ''


def stack_parameters(config: Any, image: str) -> list[str]:
    params = [
        f'ParameterKey=Protection,ParameterValue={REQUIRED_PROTECTION}',
        f'ParameterKey=WatchdogMode,ParameterValue={REQUIRED_WATCHDOG_MODE}',
        f'ParameterKey=ServerImageUri,ParameterValue={image}',
        f'ParameterKey=AdminEmail,ParameterValue={config.admin_email}',
    ]
    if config.console_bundle_version:
        params.append(f'ParameterKey=ConsoleBundleVersion,ParameterValue={config.console_bundle_version}')
    return params


def _raise_aws_error(service: str, operation: str, stderr: str) -> None:
    code = parse_aws_error_code(stderr)
    if operation == 'describe-stacks' and code in RETRYABLE_AWS_CODES:
        raise RetryableStackRead(f'aws {service} {operation} transient error ({code})')
    detail = f' ({code})' if code else ''
    raise AwsCommandError(f'aws {service} {operation} failed{detail}', error_code=code)


class AwsJsonTransport:
    """Shared region-bound AWS CLI JSON transport that redacts stderr details."""

    def __init__(
        self, region: str, timeout: int = AWS_CMD_TIMEOUT_SECONDS, runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self._region = region
        self._timeout = timeout
        self._runner = runner

    def __call__(self, *command: str) -> dict:
        service, operation, *args = command
        argv = ['aws', '--region', self._region, '--output', 'json', '--no-cli-pager',
                service, operation, *args]
        try:
            result = self._runner(
                argv, capture_output=True, text=True, timeout=self._timeout, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f'aws {service} {operation} timed out') from error
        if result.returncode != 0:
            _raise_aws_error(service, operation, result.stderr)
        return json.loads(result.stdout) if result.stdout.strip() else {}


class AwsCliAdapter:
    """Region-bound AWS CLI JSON adapter that never exposes account IDs or ARNs."""

    def __init__(self, config: Any, runner: Callable[..., Any] = subprocess.run) -> None:
        self._config = config
        self._transport = AwsJsonTransport(config.region, AWS_CMD_TIMEOUT_SECONDS, runner)
        self._account: str | None = None

    def prepare_delete(self, stack_id: str) -> None:
        disable_deletion_protection(self, stack_id)

    def scope(self) -> tuple[str, str]:
        if self._account is None:
            identity = self('sts', 'get-caller-identity')
            account = str(identity.get('Account', ''))
            require(ACCOUNT_ID_RE.fullmatch(account), 'Unable to determine caller account')
            self._account = account
        return self._account, self._config.region

    def create_stack(self, owned: OwnedAttempt, image: str) -> str:
        tags = [{'Key': 'HonuaGate', 'Value': 'true'}, *owned.tags]
        tag_args = [f'Key={tag["Key"]},Value={tag["Value"]}' for tag in tags]
        try:
            response = self(
                'cloudformation', 'create-stack',
                '--stack-name', owned.stack_name,
                '--template-url', self._config.template_url,
                '--role-arn', self._config.cfn_role_arn,
                '--capabilities', 'CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND',
                '--client-request-token', owned.token,
                '--tags', *tag_args,
                '--parameters', *stack_parameters(self._config, image),
            )
        except TimeoutError as error:
            raise AmbiguousStackCreate('CreateStack acknowledgement timed out') from error
        stack_id = response.get('StackId', '')
        require(isinstance(stack_id, str) and bool(stack_id), 'CreateStack returned no StackId')
        return stack_id

    def _delete_role_args(self, service: str, operation: str, args: tuple[str, ...]) -> list[str]:
        needs_role = (service == 'cloudformation' and operation == 'delete-stack'
                      and '--role-arn' not in args)
        return ['--role-arn', self._config.cfn_role_arn] if needs_role else []

    def __call__(self, *command: str) -> dict:
        service, operation, *args = command
        extra = self._delete_role_args(service, operation, tuple(args))
        return self._transport(service, operation, *args, *extra)


def verify_redis_off(aws: Any, summaries: list[dict]) -> None:
    for item in summaries:
        kind = str(item.get('ResourceType', ''))
        logical = str(item.get('LogicalResourceId', ''))
        require(not kind.startswith(ELASTICACHE_PREFIX) and REDIS_TOKEN not in logical.lower(),
                'Redis resource detected in stack; Launch Stack must be Redis-off')
    task_res = one([r for r in summaries if r.get('LogicalResourceId') == 'ServerTask'],
                   'Missing ServerTask stack resource')
    task_def = aws('ecs', 'describe-task-definition',
                   '--task-definition', task_res['PhysicalResourceId']).get('taskDefinition', {})
    container = server_container(task_def.get('containerDefinitions', []))
    names = [str(entry.get('name', '')) for entry in container.get('environment', [])]
    names.extend(str(entry.get('name', '')) for entry in container.get('secrets', []))
    require(all(REDIS_TOKEN not in name.lower() for name in names),
            'Redis configuration detected in ECS server task definition')


def _required_hardened_resources(summaries: list[dict]) -> dict[str, str]:
    required_types = (
        ('waf', 'AWS::WAFv2::WebACL'),
        ('distribution', 'AWS::CloudFront::Distribution'),
        ('database', 'AWS::RDS::DBInstance'),
        ('alb', 'AWS::ElasticLoadBalancingV2::LoadBalancer'),
    )
    found: dict[str, str] = {}
    for key, resource_type in required_types:
        matches = [r for r in summaries if r.get('ResourceType') == resource_type]
        entry = one(matches, f'Hardened profile requires {resource_type}')
        require(entry.get('ResourceStatus') in COMPLETE_STATUSES and entry.get('PhysicalResourceId'),
                f'Hardened resource {resource_type} is not complete')
        found[key] = entry['PhysicalResourceId']
    endpoints = [
        r for r in summaries
        if r.get('ResourceType') == 'AWS::EC2::VPCEndpoint' and r.get('ResourceStatus') in COMPLETE_STATUSES
    ]
    require(len(endpoints) > 0, 'Hardened profile requires active private VPC endpoints')
    return found


def verify_hardened_profile(aws: Any, stack: dict, summaries: list[dict]) -> None:
    resources = _required_hardened_resources(summaries)
    params = {p.get('ParameterKey'): p.get('ParameterValue') for p in stack.get('Parameters', [])}
    require(params.get('Protection') == REQUIRED_PROTECTION,
            'Stack Protection parameter must be Contain')
    outputs = {o.get('OutputKey'): o.get('OutputValue') for o in stack.get('Outputs', [])}
    if 'ProtectionStage' in outputs:
        require(outputs['ProtectionStage'] in PROTECTED_STAGES, 'Stack ProtectionStage is not hardened')
    db_resp = aws('rds', 'describe-db-instances', '--db-instance-identifier', resources['database'])
    db_instance = one(db_resp.get('DBInstances'), 'Missing RDS database instance')
    require(db_instance.get('DeletionProtection') is True, 'RDS DeletionProtection must be enabled')
    alb_resp = aws('elbv2', 'describe-load-balancers', '--load-balancer-arns', resources['alb'])
    alb = one(alb_resp.get('LoadBalancers'), 'Missing internal load balancer')
    require(alb.get('Scheme') == 'internal', 'Load balancer must be internal for Hardened profile')
    attrs = aws('elbv2', 'describe-load-balancer-attributes', '--load-balancer-arn', resources['alb'])
    attr_map = {a.get('Key'): a.get('Value') for a in attrs.get('Attributes', [])}
    require(attr_map.get('deletion_protection.enabled') == 'true',
            'ALB deletion protection must be enabled')
    dist = aws('cloudfront', 'get-distribution', '--id', resources['distribution']).get('Distribution', {})
    dist_cfg = dist.get('DistributionConfig', {})
    require(dist_cfg.get('Enabled') is True and bool(dist_cfg.get('WebACLId')),
            'CloudFront distribution must be enabled with an attached WAF WebACL')


def extract_frontend_origin(stack: dict) -> str:
    outputs = {o.get('OutputKey'): o.get('OutputValue') for o in stack.get('Outputs', [])}
    raw_url = outputs.get('HonuaUrl') or outputs.get('ConsoleUrl') or ''
    require(isinstance(raw_url, str) and raw_url.startswith('https://'),
            'Stack outputs must expose an HTTPS HonuaUrl or ConsoleUrl')
    parsed = urlsplit(raw_url)
    return f'{parsed.scheme}://{parsed.netloc}'


def _unprotect_load_balancer(aws: Any, alb_arn: str) -> None:
    try:
        aws('elbv2', 'modify-load-balancer-attributes', '--load-balancer-arn', alb_arn,
            '--attributes', 'Key=deletion_protection.enabled,Value=false')
    except AwsCommandError as error:
        if error.error_code not in NOT_FOUND_AWS_CODES:
            raise


def _try_unprotect_database_once(aws: Any, db_id: str) -> str:
    try:
        aws('rds', 'modify-db-instance', '--db-instance-identifier', db_id, '--no-deletion-protection')
        return 'done'
    except AwsCommandError as error:
        if error.error_code in NOT_FOUND_AWS_CODES:
            return 'done'
        if error.error_code == 'InvalidDBInstanceState':
            return 'retry'
        raise


def _unprotect_database(aws: Any, db_id: str, sleep: Callable[[float], None]) -> None:
    for attempt in range(1, RDS_UNPROTECT_ATTEMPTS + 1):
        if _try_unprotect_database_once(aws, db_id) == 'done':
            return
        require(attempt < RDS_UNPROTECT_ATTEMPTS,
                'RDS database remained busy while disabling deletion protection')
        sleep(RDS_UNPROTECT_POLL_SECONDS)


def _unprotect_resource(aws: Any, item: dict, sleep: Callable[[float], None]) -> None:
    physical = item.get('PhysicalResourceId')
    if not physical:
        return
    kind = item.get('ResourceType')
    if kind == 'AWS::ElasticLoadBalancingV2::LoadBalancer':
        _unprotect_load_balancer(aws, physical)
    if kind == 'AWS::RDS::DBInstance':
        _unprotect_database(aws, physical, sleep)


def disable_deletion_protection(
    aws: Any, stack_id: str, sleep: Callable[[float], None] = time.sleep,
) -> None:
    for item in resource_summaries(aws, stack_id):
        _unprotect_resource(aws, item, sleep)
