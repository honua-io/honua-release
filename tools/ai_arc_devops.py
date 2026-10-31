"""AI arc stage 1 on aws-ecs: the honua-devops operator, through the merged #1165 install harness.

The arc's AwsSetup stage delegates here (``arc_stage``). Everything product-side is #1165's, unchanged:
``StdioOperator`` (the pinned v2026.1.2 operator over MCP), ``ApprovalIssuer`` (KMS-MAC customer approval of
the exact plan), ``AwsController`` (slot identity, AWS observations, teardown), the contract-bound least
privilege checks and ``InstallHarness`` (plan review, approval binding, apply gating, handoff
verification). This adapter only:

  * builds those pieces from the release cell's slot file exactly as ``live_case`` does, without a
    model session of its own: the arc's session drives the harness through one ``honua_devops`` backend;
  * gives the customer the cell details as plain notes (no tool names);
  * scores the stage from the harness's own observations without tearing the cell down (the arc needs
    it for ops, services and Studio); teardown is a slot closer that runs when the arc run ends;
  * reports the harness's approval as an arc approval bound to the plan, slot and deploy identity.

EKS and Lambda stay unavailable: #1165 keeps their first installs fail-closed (product gaps).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

GOALS = {'aws-ecs': 'city-gis-ecs'}


def arc_stage(ctx):
    return DevopsSetup(GOALS[ctx.target]) if ctx.target in GOALS else None


class HarnessBackend:
    """The arc's view of #1165's InstallHarness: one product MCP server (honua_devops)."""

    def __init__(self, harness, view, operator):
        self.harness, self.view, self.operator = harness, view, operator
        self.calls = 0
        self.approvals = []

    def tool_view(self):
        return self.view

    def execute(self, kind, action):
        self.calls += 1
        before = self.harness.approval
        result = self.harness.execute(kind, dict(action))
        if self.harness.approval is not None and self.harness.approval is not before:
            self.approvals.append({'approval': self.harness.approval, 'afterAction': self.calls})
        return result

    def close(self):
        self.operator.close()


class DevopsSetup:
    """StageModule for aws-ecs setup (duck-typed to ai_arc_runner.StageModule)."""
    stage, lane = 'setup', '#1165 honua-devops operator install harness'
    owns_operator = True      # #1165 installs, binds and runs the operator itself

    def __init__(self, goal_id):
        self.goal_id = goal_id
        self.harness = self.backend = None

    # -- availability ------------------------------------------------------------------------
    def unavailable(self, ctx):
        import agent_e2e_aws_install as aws
        missing = [k for k in ('slot_receipt', 'bootstrap_outputs', 'operator_binary') if not ctx.options.get(k)]
        if missing:
            return 'the release cell slot is incomplete: ' + ', '.join(missing)
        blockers = aws.blocking_gaps(self.goal_id)
        if blockers:
            return 'honua-devops first-install product gaps: ' + ', '.join(map(str, blockers))
        try:
            __import__('agent_e2e_cli_sessions')
        except ImportError:
            return 'the CLI corpus sessions (#1160) are not installed'
        return None

    # -- harness-owned slot work ---------------------------------------------------------------
    def prepare(self, ctx):
        """``live_case``'s construction, minus the model session and the teardown."""
        import boto3
        import yaml
        import agent_e2e_aws_install as aws
        from agent_e2e_aws_controller import (ApprovalIssuer, AwsController, ControllerError, HttpProbes,
                                              StdioOperator)
        slot = json.loads(Path(ctx.options['slot_receipt']).read_text())
        goal = aws.load_goal(self.goal_id)
        contract, digest = aws.load_contract(slot['contractPath'], goal['topology'])
        if digest != slot['contractDigest']:
            raise ControllerError('contract-changed')
        exported = json.loads(Path(ctx.options['bootstrap_outputs']).read_text())
        mac = exported['approval_mac_contract']
        signers = mac['separation']['signer_role_arns']
        if (mac['kind'] != 'honua.iac.approval-mac-key' or not mac['enabled'] or len(signers) != 1
                or mac['separation']['verifier_can_sign'] is not False
                or contract['role_arn'] not in mac['separation']['verifier_role_arns']
                or contract['role_arn'] in signers):
            raise ControllerError('approval-role-separation')
        controller = AwsController(boto3.Session(region_name='us-east-1'), slot, contract)
        run_id = slot['runId']
        creds = controller.assume(contract['role_arn'], 'arc-install-' + run_id)
        approver_creds = controller.assume(signers[0], 'arc-approve-' + run_id)
        work = ctx.private_dir / 'setup' / 'aws'
        for directory in ('operator-data', 'operator-tmp'):
            (work / directory).mkdir(parents=True, mode=0o700, exist_ok=True)
        env = aws.operator_env(creds, contract_path=slot['contractPath'], iac_root=slot['iacRoot'],
                               issuer_keys=exported['approval_mac_issuer_key_arns'], workdir=work)
        proxy = yaml.safe_load(aws.MANIFEST.read_text())['clientArtifacts']['honua-mcp-server']
        env['HONUA_DEVOPS_MCP_PROXY_PACKAGE'] = '@honua/mcp-server@' + str(proxy['version'])
        env['HONUA_DEVOPS_MCP_PROXY_INTEGRITY'] = proxy['integrity']
        issuer_env = {k: v for k, v in env.items() if k not in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                                                                  'AWS_SESSION_TOKEN')}
        issuer_env.update(AWS_ACCESS_KEY_ID=approver_creds.access_key_id,
                          AWS_SECRET_ACCESS_KEY=approver_creds.secret_access_key,
                          AWS_SESSION_TOKEN=approver_creds.session_token)
        approver = ApprovalIssuer(ctx.options['operator_binary'], issuer_env, work / 'approver',
                                  signers[0].rsplit('/', 1)[-1])
        operator = StdioOperator(ctx.options['operator_binary'], work / 'operator')
        operator.bind(env, slot['iacRoot'], creds)
        variables = {line.split(' = ', 1)[0]: json.loads(line.split(' = ', 1)[1])
                     for line in Path(slot['variablesFile']).read_text().splitlines()}
        secrets = creds.secrets() + approver_creds.secrets() + [variables['honua_admin_password'],
                                                                variables['honua_connection_encryption_master_key']]
        ctx.slot.harness_secrets.extend(s for s in secrets if s not in ctx.slot.harness_secrets)
        self.prefix = aws.name_prefix_for(run_id, goal['topology'])
        self.hostname = slot['hostname']
        self.harness = aws.InstallHarness(
            goal=goal, operator=operator, probes=HttpProbes(), creds=creds, prefix=self.prefix,
            hostname=self.hostname, module_dir=slot['iacRoot'], secrets=secrets, contract=contract,
            contract_digest=digest, approver=approver, controller=controller, cell_domain=self.hostname)
        self.backend = HarnessBackend(self.harness, aws.tool_view(operator.catalog(), scenario='aws-install'),
                                      operator)
        # The arc keeps the cell for the later stages; the cell is torn down when the arc run ends.
        ctx.slot.closers.append(self._teardown)
        ctx.slot.facts['operator'] = {'tag': aws.DEVOPS_TAG, 'commit': aws.DEVOPS_SHA,
                                      'archiveSha256': aws.DEVOPS_ARCHIVE_SHA256}
        ctx.slot.facts['operator']['deployIdentity'] = {'sha256': 'sha256:' + digest, 'roleArn': contract['role_arn'],
                                                        'topology': goal['topology'].as_dict()}
        ctx.slot.facts.update(account=aws.ACCOUNT, region='us-east-1', cellHostname=self.hostname)
        if slot.get('consoleHostname'):
            # The honua-iac aws-ecs module's Console service (when the cell deploys one).
            ctx.slot.facts['consoleBaseUrl'] = 'https://' + slot['consoleHostname']
        ctx.slot.controller['admin_key'] = variables['honua_admin_password']
        self.variables = variables

    def _teardown(self):
        if self.harness is not None:
            self.harness.observed['teardownRemaining'] = self.harness.aws.destroy()

    def notes(self, ctx):
        import agent_e2e_aws_install as aws
        return (f'Our release cell hostname is {self.hostname}. Please use the name prefix {self.prefix}, '
                f'region us-east-1 and environment {aws.ENVIRONMENT}, and install the release image '
                f'{aws.INSTALL_IMAGE}. Wait for my approval of the exact plan before building anything.')

    def attachment(self, ctx):
        return None

    def backends(self, ctx):
        return {'honua_devops': self.backend}

    # -- observation ---------------------------------------------------------------------------
    def observe(self, ctx, row):
        """#1165's own checks, mapped to the arc's setup contract (the cell stays up)."""
        from ai_arc_runner import assert_row
        self.harness.probe()
        status, attribution, reason, checks = self.harness.score(row.get('finalAnswer'))
        ok = {name: bool(check.get('pass')) for name, check in checks.items()}
        evidence = {'status': status, 'attribution': attribution, 'reason': reason, 'checks': ok,
                    'observed': {k: v for k, v in self.harness.observed.items() if k != 'teardownRemaining'}}
        if self.hostname:
            ctx.slot.controller['base_url'] = f'https://{self.hostname}'
        anonymous = None
        try:
            anonymous = self.harness.probes.get(f'https://{self.hostname}/api/v1/admin/version')
        except Exception:  # noqa: BLE001
            anonymous = None
        anonymous_status = anonymous[0] if isinstance(anonymous, tuple) else anonymous
        return [assert_row('install.identity', ok['install_release'], evidence),
                assert_row('deployment.ready', ok['health'], evidence),
                assert_row('authentication.required', anonymous_status in (401, 403), {'anonymousStatus': anonymous_status}),
                assert_row('topology.matches-goal', ok['topology'], evidence),
                assert_row('deploy-identity.least-privilege', ok['actions'] and ok['admin'], evidence),
                assert_row('credentials.short-lived', ok['long_lived'] and ok['principal'], evidence),
                assert_row('handoff.verified', ok['handoff'], evidence)]

    def handoff_observations(self, ctx, answer):
        served = self.harness is not None and self.harness.observed.get('handoffVerified')
        url = f'https://{self.hostname}' if self.hostname else None
        status = 200 if served and self.harness.observed.get('healthReady') == 200 else None
        return {'resources': [{'url': url, 'served': bool(status), 'httpStatus': status}] if url else [],
                'accessProbes': [], 'facts': {}}

    def deployment_hosts(self, ctx):
        return (self.hostname,) if self.hostname else ()

    def approvals(self, ctx):
        """The harness's KMS-MAC approval, bound to the plan, slot, deploy identity, account and region."""
        out = []
        events = [e for e in (self.harness.controller if self.harness else []) if e.get('event') == 'approval-issued']
        for issued, event in zip(self.backend.approvals if self.backend else [], events):
            approval = issued['approval']
            from ai_arc_safety import canonical, sha256
            out.append({'receiptSha256': sha256(canonical(approval)), 'afterAction': issued['afterAction'],
                        'operationId': approval.get('provisioningOperationId'), 'action': approval.get('action'),
                        'planDigest': event.get('planSha256'), 'stack': approval.get('stack'),
                        'environment': approval.get('environment'), 'region': event.get('region'),
                        'account': event.get('account'), 'slotId': ctx.slot.project,
                        'deployIdentitySha256': 'sha256:' + str(event.get('contractDigest'))})
        return out

    def scope(self, ctx):
        ops = [e.get('approval', {}).get('provisioningOperationId') for e in (self.harness.controller if self.harness
                                                                                    else []) if e.get('approval')]
        return {o for o in ops if o}
