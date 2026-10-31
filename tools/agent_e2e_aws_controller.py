"""Trusted AWS install controller. Never exposed as a model tool.

Only a trunk workflow may call the live methods. Unit tests inject SDK clients;
no AWS credentials, SDK calls, Terraform mutations or CLI models run in tests.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import subprocess
import threading
import time
from datetime import datetime, timezone


class ControllerError(RuntimeError):
    pass


def run_checked(argv, *, env=None, cwd=None, timeout=1200):
    # Captured output can contain plan values or credentials. Never echo it.
    result = subprocess.run(argv, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise ControllerError('controller-command-failed:' + Path(argv[0]).name)
    return result.stdout


def matches_role(arn, role):
    parts = role.split(':')
    if len(parts) != 6:
        return False
    return bool(re.fullmatch(
        re.escape(f'arn:{parts[1]}:sts::{parts[4]}:assumed-role/{role.rsplit("/", 1)[-1]}/')
        + r'[A-Za-z0-9+=,.@_-]+', arn or ''))


class StdioOperator:
    """The published MCP binary, using its actual tools/list and tools/call wire."""

    def __init__(self, binary, workdir):
        self.binary, self.workdir = str(Path(binary).resolve()), Path(workdir)
        self.process = None
        self.sequence = 0
        self.lock = threading.Lock()

    def bind(self, env, module_dir, creds):
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.log = open(self.workdir / 'operator.stderr', 'w', encoding='utf-8')
        os.chmod(self.log.name, 0o600)
        self.process = subprocess.Popen([self.binary, '--mcp'], cwd=self.workdir, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, bufsize=0)
        self.pending = b''
        self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
            'clientInfo': {'name': 'honua-aws-install-eval', 'version': '1'}})
        self.process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        self.process.stdin.flush()

    def rpc(self, method, params):
        with self.lock:
            self.sequence += 1
            seq = self.sequence
            request = {'jsonrpc': '2.0', 'id': seq, 'method': method, 'params': params}
            self.process.stdin.write((json.dumps(request) + '\n').encode())
            self.process.stdin.flush()
            deadline = time.monotonic() + 1500
            while True:
                while b'\n' in self.pending:
                    raw, self.pending = self.pending.split(b'\n', 1)
                    response = json.loads(raw)
                    if response.get('id') == seq:
                        if 'error' in response:
                            raise ControllerError('operator-rpc-error')
                        return response['result']
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                    raise ControllerError('operator-timeout')
                chunk = os.read(self.process.stdout.fileno(), 65536)
                if not chunk:
                    raise ControllerError('operator-exited')
                self.pending += chunk
                if len(self.pending) > 8 * 1024 * 1024:
                    raise ControllerError('operator-response-too-large')

    def catalog(self):
        return self.rpc('tools/list', {})

    def call(self, name, args):
        result = self.rpc('tools/call', {'name': name, 'arguments': args})
        if result.get('isError'):
            raise ControllerError('operator-tool-error')
        if isinstance(result.get('structuredContent'), dict):
            return result['structuredContent']
        for item in result.get('content', []):
            if item.get('type') == 'text':
                try:
                    return json.loads(item['text'])
                except (ValueError, KeyError):
                    continue
        raise ControllerError('operator-result-invalid')

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            self.log.close()
            self.process.stdin.close()
            self.process.stdout.close()


class ApprovalIssuer:
    """Delegate canonicalization/signing to the shipped issuer command."""

    def __init__(self, binary, env, workdir, issuer, mode='kms-mac', environment='it'):
        if mode not in ('kms-mac', 'local-hmac-dev') or (mode == 'local-hmac-dev' and environment != 'dev'):
            raise ControllerError('approval-mode-invalid')
        self.binary, self.env, self.workdir = str(binary), env, Path(workdir)
        self.issuer, self.mode, self.environment = issuer, mode, environment

    def issue(self, plan):
        self.workdir.mkdir(parents=True, exist_ok=True)
        path = self.workdir / 'reviewed-plan.json'
        path.write_text(json.dumps(plan), encoding='utf-8')
        path.chmod(0o600)
        raw = run_checked([self.binary, '--issue-provision-approval', '--from-plan-response', str(path),
            '--action', 'apply', '--signing-mode', self.mode, '--issuer', self.issuer,
            '--stack', 'aws-ecs', '--environment', self.environment], env=self.env)
        result = json.loads(raw)
        if result.get('decision') != 'approved' or result.get('signingMode') != self.mode or not result.get('signature'):
            raise ControllerError('approval-not-signed')
        return result


class AwsController:
    """Keeps the workflow OIDC session separate from operator and issuer sessions."""

    def __init__(self, session, slot, contract, *, sleep=time.sleep):
        self.session, self.slot, self.contract, self.sleep = session, slot, contract, sleep
        self.started = datetime.now(timezone.utc)
        identity = session.client('sts').get_caller_identity()
        self.principal_arn = identity['Arn']
        from agent_e2e_aws_install import ACCOUNT, PROVISION_ROLE_ARN
        if identity['Account'] != ACCOUNT or not matches_role(identity['Arn'], PROVISION_ROLE_ARN):
            raise ControllerError('controller-principal')

    def assume(self, role, name):
        from agent_e2e_aws_install import ScopedCredentials
        if matches_role(getattr(self, 'principal_arn', None), role):
            frozen = self.session.get_credentials().get_frozen_credentials()
            response = {'AccessKeyId': frozen.access_key, 'SecretAccessKey': frozen.secret_key,
                        'SessionToken': frozen.token}
        else:
            response = self.session.client('sts').assume_role(RoleArn=role, RoleSessionName=name,
                DurationSeconds=3600)['Credentials']
        if not response['AccessKeyId'].startswith('ASIA') or not response.get('SessionToken'):
            raise ControllerError('sts-required')
        return ScopedCredentials(response['AccessKeyId'], response['SecretAccessKey'], response['SessionToken'], role)

    def observation(self, creds):
        import boto3
        caller = boto3.Session(aws_access_key_id=creds.access_key_id,
            aws_secret_access_key=creds.secret_access_key, aws_session_token=creds.session_token,
            region_name='us-east-1').client('sts').get_caller_identity()['Arn']
        # CloudTrail is eventually consistent. Only collect this unique deploy session;
        # reads use the controller, never the operator's response or deploy credentials.
        trail = self.session.client('cloudtrail')
        arns = []
        for attempt in range(6):
            arns = []
            for page in trail.get_paginator('lookup_events').paginate(StartTime=self.started,
                    LookupAttributes=[{'AttributeKey': 'AccessKeyId', 'AttributeValue': creds.access_key_id}]):
                for row in page.get('Events', []):
                    event = json.loads(row['CloudTrailEvent'])
                    identity = event.get('userIdentity', {})
                    arn = identity.get('arn', '')
                    arns.append(arn)
            if arns:
                break
            if attempt < 5:
                self.sleep(30)
        return {'callerArn': caller, 'cloudtrailArns': arns}

    def inventory(self):
        tagging = self.session.client('resourcegroupstaggingapi')
        found = set()
        # Two independent tag queries catch both canonical release-cell tag conventions.
        for key, value in [('validationRunId', self.slot['validationRunId']),
                           ('name_prefix', self.slot['namePrefix'])]:
            for page in tagging.get_paginator('get_resources').paginate(
                    TagFilters=[{'Key': key, 'Values': [value]}]):
                found.update(item['ResourceARN'] for item in page.get('ResourceTagMappingList', []))
        # IAM resources are not covered by Resource Groups Tagging API.
        iam = self.session.client('iam')
        for page in iam.get_paginator('list_roles').paginate():
            for role in page.get('Roles', []):
                tags = {t['Key']: t['Value'] for t in iam.list_role_tags(RoleName=role['RoleName'])['Tags']}
                if (tags.get('validationRunId') == self.slot['validationRunId']
                        or tags.get('name_prefix') == self.slot['namePrefix']):
                    found.add(role['Arn'])
        for page in iam.get_paginator('list_policies').paginate(Scope='Local'):
            for policy in page.get('Policies', []):
                tags = {t['Key']: t['Value'] for t in iam.list_policy_tags(PolicyArn=policy['Arn'])['Tags']}
                if (tags.get('validationRunId') == self.slot['validationRunId']
                        or tags.get('name_prefix') == self.slot['namePrefix']):
                    found.add(policy['Arn'])
        return sorted(found)

    def installed_image(self):
        cluster = self.slot['namePrefix'] + '-it-cluster'
        ecs = self.session.client('ecs')
        services = ecs.list_services(cluster=cluster)['serviceArns']
        if len(services) != 1:
            raise ControllerError('installed-service')
        description = ecs.describe_services(cluster=cluster, services=services)['services'][0]
        definition = ecs.describe_task_definition(taskDefinition=description['taskDefinition'])['taskDefinition']
        images = [c['image'] for c in definition['containerDefinitions'] if c.get('name') == 'honua']
        if len(images) != 1:
            raise ControllerError('installed-image')
        return images[0]

    def destroy(self):
        root = Path(self.slot['terraformRoot']).resolve()
        if root != Path(self.slot['iacRoot']).resolve() / 'infrastructure/terraform/examples/aws':
            raise ControllerError('teardown-root')
        # Run with the workflow's provision/reaper session, which owns the whole cell.
        # The retained variable file and backend are the ones prepared before the agent.
        run_checked(['terraform', '-chdir=' + str(root), 'init', '-input=false', '-no-color'])
        run_checked(['terraform', '-chdir=' + str(root), 'destroy', '-auto-approve',
                     '-input=false', '-no-color', '-var-file=' + self.slot['variablesFile']])
        for attempt in range(6):
            remaining = self.inventory()
            if not remaining:
                return remaining
            if attempt < 5:
                self.sleep(15)
        return remaining


class HttpProbes:
    def get(self, url):
        from urllib.request import build_opener, HTTPRedirectHandler
        from urllib.error import HTTPError
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None
        try:
            with build_opener(NoRedirect).open(url, timeout=30) as response:
                return response.status
        except HTTPError as exc:
            return exc.code


def export_bootstrap(iac_root, backend_config, output):
    """Read existing bootstrap state; this never creates the bootstrap resources."""
    root = Path(iac_root).resolve() / 'infrastructure/terraform/bootstrap/aws-exec-identity'
    (root / 'backend.tf').write_text('terraform {\n  backend "s3" {}\n}\n')
    run_checked(['terraform', '-chdir=' + str(root), 'init', '-input=false', '-no-color',
                 '-backend-config=' + str(Path(backend_config).resolve())])
    raw = run_checked(['terraform', '-chdir=' + str(root), 'output', '-json'])
    outputs = json.loads(raw)
    exported = {key: outputs[key]['value'] for key in ('deploy_identity_contract',
        'approval_mac_contract', 'approval_mac_issuer_key_arns', 'workload_permissions_boundary_arn')}
    path = Path(output)
    path.write_text(json.dumps(exported, indent=2) + '\n')
    path.chmod(0o600)
    return exported


def prepare_slot(args):
    """Persist the reaper handle before the operator gets any credentials."""
    import secrets
    import base64
    import boto3
    import agent_e2e_aws_install as aws
    root = Path(args.iac_root).resolve()
    terraform_root = root / 'infrastructure/terraform/examples/aws'
    goal = aws.load_goal(args.goal_id)
    if aws.blocking_gaps(args.goal_id):
        raise ControllerError('product-gap')
    hostname = args.hostname or aws.cell_hostname(args.run_id)
    aws.assert_release_cell(role_arn=args.role_arn, repository=args.repository, lifecycle=args.lifecycle,
        dns_parent=args.dns_parent, hostname=hostname)
    exported = json.loads(Path(args.bootstrap_outputs).read_text())
    contract_path = Path(args.contract_path).resolve()
    # Preserve exactly these bytes for the digest subsequently tagged into the plan.
    contract_path.write_text(json.dumps(exported['deploy_identity_contract'], separators=(',', ':')))
    contract, digest = aws.load_contract(contract_path, goal['topology'])
    prefix = aws.name_prefix_for(args.run_id, goal['topology'])
    slot = {'runId': args.run_id, 'goal': args.goal_id, 'owner': aws.OWNER,
        'hostname': hostname, 'validationRunId': aws.validation_run_id(args.run_id, goal['topology']),
        'namePrefix': prefix, 'iacRoot': str(root), 'terraformRoot': str(terraform_root),
        'contractPath': str(contract_path), 'contractDigest': digest,
        'variablesFile': str(terraform_root / 'terraform.tfvars')}
    controller = AwsController(boto3.Session(region_name='us-east-1'), slot, contract)
    if controller.inventory():
        raise ControllerError('slot-not-empty')
    aws._write_exclusive(args.slot_file, slot)
    bucket = os.environ['HONUA_AWS_CELL_STATE_BUCKET']
    backend_role = os.environ['HONUA_AWS_CELL_BACKEND_ROLE_ARN']
    if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket):
        raise ControllerError('state-bucket')
    if not re.fullmatch(r'arn:aws:iam::' + aws.ACCOUNT + r':role/[A-Za-z0-9+=,.@_/-]+', backend_role):
        raise ControllerError('state-role')
    # Activation files are gitignored in honua-iac; the committed source stays pinned.
    (terraform_root / 'backend.tf').write_text('terraform {\n backend "s3" {\n'
        + f' bucket = {json.dumps(bucket)}\n key = "agent-e2e/aws-install/{prefix}/terraform.tfstate"\n'
        + ' region = "us-east-1"\n encrypt = true\n use_lockfile = true\n'
        + f' assume_role = {{ role_arn = {json.dumps(backend_role)} }}\n' + ' }\n}\n')
    identity = json.loads(os.environ['HONUA_AWS_INSTALL_OPERATOR_CONTRACT_IDENTITY'])
    if (identity.get('image_reference') != aws.INSTALL_IMAGE
            or identity.get('image_digest') != aws.INSTALL_IMAGE.split('@')[-1]
            or identity.get('iac_revision') != aws.honua_iac_sha(aws.MANIFEST.read_text())):
        raise ControllerError('install-contract-pins')
    variables = {'honua_admin_password': secrets.token_urlsafe(32),
        'honua_connection_encryption_master_key': base64.b64encode(secrets.token_bytes(32)).decode(),
        'permissions_boundary_arn': exported['workload_permissions_boundary_arn'],
        'region': 'us-east-1', 'environment': aws.ENVIRONMENT, 'name_prefix': prefix,
        'honua_image': aws.INSTALL_IMAGE, 'redis_enabled': goal['topology'].redis,
        'rds_deletion_protection': False, 'alb_deletion_protection': False,
        'alb_access_logs_enabled': False, 'alb_access_logs_force_destroy': True,
        'canary_enabled': False, 'desired_count': 1, 'max_capacity': 1,
        'tags': {'validationRunId': slot['validationRunId'], 'name_prefix': prefix,
                 'owner': aws.OWNER, 'lifecycle': 'ephemeral'},
        'operator_contract_identity': identity}
    if goal['topology'].custom_domain:
        variables.update(domain_name=hostname, route53_zone_id=os.environ['HONUA_AWS_CELL_ROUTE53_ZONE_ID'])
    # HCL assignments accept JSON expressions for nested values. This filename is
    # gitignored by the producer and automatically consumed by the real operator.
    secret_path = terraform_root / 'terraform.tfvars'
    secret_path.write_text('\n'.join(f'{key} = {json.dumps(value)}' for key, value in variables.items()) + '\n')
    secret_path.chmod(0o600)
    return slot


def live_case(args):
    import boto3
    import agent_e2e_aws_install as aws
    slot = json.loads(Path(args.slot_file).read_text())
    aws.assert_release_cell(role_arn=args.role_arn, repository=args.repository, lifecycle=args.lifecycle,
        dns_parent=args.dns_parent, hostname=slot['hostname'], owner=slot['owner'])
    if slot['runId'] != args.run_id or slot['goal'] != args.goal_id:
        raise ControllerError('slot-binding')
    contract, digest = aws.load_contract(slot['contractPath'], aws.load_goal(args.goal_id)['topology'])
    if digest != slot['contractDigest']:
        raise ControllerError('contract-changed')
    exported = json.loads(Path(args.bootstrap_outputs).read_text())
    mac = exported['approval_mac_contract']
    signers = mac['separation']['signer_role_arns']
    if (mac['kind'] != 'honua.iac.approval-mac-key' or not mac['enabled'] or len(signers) != 1
            or mac['separation']['verifier_can_sign'] is not False
            or contract['role_arn'] not in mac['separation']['verifier_role_arns']
            or contract['role_arn'] in signers):
        raise ControllerError('approval-role-separation')
    controller = AwsController(boto3.Session(region_name='us-east-1'), slot, contract)
    creds = controller.assume(contract['role_arn'], 'aws-install-' + args.run_id)
    approver_creds = controller.assume(signers[0], 'aws-approve-' + args.run_id)
    work = Path(args.output).resolve().parent / ('work-' + args.run_id)
    work.mkdir(parents=True, exist_ok=True)
    for directory in ('operator-data', 'operator-tmp'):
        (work / directory).mkdir(mode=0o700, exist_ok=True)
    env = aws.operator_env(creds, contract_path=slot['contractPath'], iac_root=slot['iacRoot'],
        issuer_keys=exported['approval_mac_issuer_key_arns'], workdir=work)
    # Proxy artifacts are pinned by the checked-out platform manifest.
    import yaml
    manifest = yaml.safe_load(aws.MANIFEST.read_text())
    proxy = manifest['clientArtifacts']['honua-mcp-server']
    env['HONUA_DEVOPS_MCP_PROXY_PACKAGE'] = '@honua/mcp-server@' + str(proxy['version'])
    env['HONUA_DEVOPS_MCP_PROXY_INTEGRITY'] = proxy['integrity']
    issuer_env = {key: value for key, value in env.items() if key not in (
        'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')}
    issuer_env.update(AWS_ACCESS_KEY_ID=approver_creds.access_key_id,
        AWS_SECRET_ACCESS_KEY=approver_creds.secret_access_key, AWS_SESSION_TOKEN=approver_creds.session_token)
    issuer = signers[0].rsplit('/', 1)[-1]
    approver = ApprovalIssuer(args.operator_binary, issuer_env, work / 'approver', issuer)
    operator = StdioOperator(args.operator_binary, work / 'operator')
    try:
        operator.bind(env, slot['iacRoot'], creds)
        view = aws.tool_view(operator.catalog(), scenario='aws-install')
        session = aws.open_session(args.agent, args.model)
        # Only parse the controller-owned HCL assignments; values are JSON expressions.
        variables = {line.split(' = ', 1)[0]: json.loads(line.split(' = ', 1)[1])
            for line in Path(slot['variablesFile']).read_text().splitlines()}
        return aws.run_case(goal_id=args.goal_id, agent=args.agent, model=args.model,
            lock_digest=args.lock_digest, output=args.output, run_id=args.run_id,
            role_arn=args.role_arn, repository=args.repository, lifecycle=args.lifecycle,
            dns_parent=args.dns_parent, hostname=slot['hostname'], session=session, operator=operator,
            probes=HttpProbes(), module_source=slot['iacRoot'], workdir=work, creds=creds,
            contract=contract, contract_digest=digest, approver=approver, controller=controller,
            extra_secrets=approver_creds.secrets() + [variables['honua_admin_password'],
                variables['honua_connection_encryption_master_key']], tool_catalog=view,
            cell_domain=slot['hostname'], route53_zone_id=variables.get('route53_zone_id'))
    finally:
        operator.close()
