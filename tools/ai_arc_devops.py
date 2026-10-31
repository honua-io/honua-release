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


class OperatorOwner:
    """Owns the operator process from before it starts: registered in the stage's live set first, so
    finalize_stage stops it whatever fails later in preparation (catalog, harness, deadline). Once
    closed it refuses to start, and a start that races a close is stopped at once."""

    def __init__(self, operator, finalized=lambda: False):
        self.operator, self._closed, self.finalized = operator, False, finalized

    @property
    def closed(self):
        # A preparation that overran its deadline keeps running in its worker after finalize_stage:
        # once the stage is finalized nothing may start, even if this owner was never closed.
        return self._closed or bool(self.finalized())

    def bind(self, *args):
        if self.closed:
            raise RuntimeError('the stage was finalized before the operator started')
        self.operator.bind(*args)
        if self.closed:
            self._stop()
            raise RuntimeError('the stage was finalized while the operator started')

    def alive(self) -> bool:
        process = getattr(self.operator, 'process', None)
        return process is not None and process.poll() is None

    def _stop(self):
        if getattr(self.operator, 'process', None) is not None:
            self.operator.close()

    def close(self):
        already, self._closed = self._closed, True
        if not already:
            self._stop()
        if self.alive():
            raise RuntimeError('the operator process is still running after close')


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


def verified_operator(ctx) -> dict:
    """The published honua-devops release archive, verified against the arc's pinned sha256 (and the
    #1165 constants), extracted, and its binary digest observed. A supplied ``operator_binary`` must be
    byte-identical to the verified archive's binary; otherwise nothing is launched."""
    import hashlib
    import agent_e2e_aws_install as aws
    import ai_arc_operator
    spec = ctx.arc['operator']
    asset = ai_arc_operator.pinned_asset(spec)
    if (spec['tag'], spec['commit'], asset['sha256']) != (aws.DEVOPS_TAG, aws.DEVOPS_SHA, aws.DEVOPS_ARCHIVE_SHA256):
        raise ai_arc_operator.OperatorError('the arc operator pin differs from the install harness pin')
    installed = ai_arc_operator.install(spec, ctx.private_dir / 'operator', download=ctx.options.get('operator_download'))
    supplied = ctx.options.get('operator_binary')
    if supplied:
        digest = 'sha256:' + hashlib.sha256(Path(supplied).read_bytes()).hexdigest()
        if digest != installed['binarySha256']:
            raise ai_arc_operator.OperatorError('the supplied operator binary is not the verified release binary')
    return installed


class DevopsSetup:
    """StageModule for aws-ecs setup (duck-typed to ai_arc_runner.StageModule)."""
    stage, lane = 'setup', '#1165 honua-devops operator install harness'
    owns_operator = True      # #1165 installs, binds and runs the operator itself

    def __init__(self, goal_id):
        self.goal_id = goal_id
        self.harness = self.backend = self.controller = None

    # -- availability ------------------------------------------------------------------------
    def unavailable(self, ctx):
        import agent_e2e_aws_install as aws
        missing = [k for k in ('slot_receipt', 'bootstrap_outputs') if not ctx.options.get(k)]
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
        # Verify the release binary before any credential exists (Astra #1177 item 3).
        installed = verified_operator(ctx)
        binary = installed['binary']
        ctx.slot.facts['operator'] = {'tag': installed['tag'], 'commit': installed['commit'],
                                      'archiveSha256': installed['archiveSha256'],
                                      'binarySha256': installed['binarySha256'], 'verified': 'archive+binary'}
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
        self.controller = controller
        # The cell is torn down when the arc run ends, whatever fails from here on.
        ctx.slot.closers.append(self._teardown)
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
        # platform-manifest.yaml is release-owned (outside the frozen harness digest): record the pin used.
        ctx.slot.facts['operator']['mcpProxy'] = {'package': env['HONUA_DEVOPS_MCP_PROXY_PACKAGE'],
                                                  'integrity': proxy['integrity']}
        issuer_env = {k: v for k, v in env.items() if k not in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
                                                                  'AWS_SESSION_TOKEN')}
        issuer_env.update(AWS_ACCESS_KEY_ID=approver_creds.access_key_id,
                          AWS_SECRET_ACCESS_KEY=approver_creds.secret_access_key,
                          AWS_SESSION_TOKEN=approver_creds.session_token)
        approver = ApprovalIssuer(binary, issuer_env, work / 'approver', signers[0].rsplit('/', 1)[-1])
        operator = OperatorOwner(StdioOperator(binary, work / 'operator'), lambda: ctx.finalized is not None)
        ctx.live.append(operator)                 # owned by finalize_stage before it can start
        operator.bind(env, slot['iacRoot'], creds)
        variables = {line.split(' = ', 1)[0]: json.loads(line.split(' = ', 1)[1])
                     for line in Path(slot['variablesFile']).read_text().splitlines()}
        secrets = creds.secrets() + approver_creds.secrets() + [variables['honua_admin_password'],
                                                                variables['honua_connection_encryption_master_key']]
        ctx.slot.harness_secrets.extend(s for s in secrets if s not in ctx.slot.harness_secrets)
        self.prefix = aws.name_prefix_for(run_id, goal['topology'])
        self.hostname = slot['hostname']
        self.harness = aws.InstallHarness(
            goal=goal, operator=operator.operator, probes=HttpProbes(), creds=creds, prefix=self.prefix,
            hostname=self.hostname, module_dir=slot['iacRoot'], secrets=secrets, contract=contract,
            contract_digest=digest, approver=approver, controller=controller, cell_domain=self.hostname)
        self.backend = HarnessBackend(self.harness, aws.tool_view(operator.operator.catalog(), scenario='aws-install'),
                                      operator)
        ctx.slot.facts['operator']['deployIdentity'] = {'sha256': 'sha256:' + digest, 'roleArn': contract['role_arn'],
                                                        'topology': goal['topology'].as_dict()}
        ctx.slot.facts.update(account=aws.ACCOUNT, region='us-east-1', cellHostname=self.hostname)
        if slot.get('console'):
            # honua-iac #359: Console on the same HTTPS hostname as the server, path-routed behind the OIDC
            # edge (honua-devops #248 preset). /version.json sits behind the edge, so health is the edge's own
            # sign-in page (routed /oauth2/*, served unauthenticated); sign-in is the adapter's probe.
            ctx.slot.facts['consoleBaseUrl'] = 'https://' + slot.get('consoleHostname', self.hostname)
            ctx.slot.facts['consoleHealthPath'] = '/oauth2/sign_in'
        ctx.slot.controller['admin_key'] = variables['honua_admin_password']
        self.variables = variables

    def _teardown(self):
        """Destroy the cell, then a live inventory read decides: clean only when nothing tagged for the
        cell remains. Residue or an inventory that cannot be read makes the run's teardown unclean."""
        controller = self.controller
        if controller is None:
            return {'status': 'clean'}
        try:
            remaining = controller.destroy()
        finally:
            if self.harness is not None:
                self.harness.observed['teardownRemaining'] = None
        try:
            inventory = sorted(controller.inventory())
        except Exception as exc:  # noqa: BLE001
            return {'status': 'unknown', 'inventory': f'unreadable ({type(exc).__name__})',
                    'remaining': sorted(remaining or [])}
        if self.harness is not None:
            self.harness.observed['teardownRemaining'] = inventory
        return {'status': 'clean' if not inventory and not remaining else 'residual',
                'remaining': sorted(set(inventory) | set(remaining or []))}

    def harness_unsafe(self, ctx):
        return list(self.harness.unsafe) if self.harness is not None else []

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
