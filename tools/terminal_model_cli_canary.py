"""Native CLI mode for the terminal journey, with the original independent verifier.

CLI provenance is a separate exploratory receipt, not signed Bedrock provenance.
Datasource connection and upload are explicit controller fixture work because
these arms expose only native MCP tools, with no SDK shell bridge.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tempfile

from agent_cli_sessions import CliFailure, CliModelSession, digest
from agent_e2e_cli_runner import target_url
import agent_e2e_local_runner as corpus
import terminal_model_canary as canary


def run_canary(*, driver, cli, journey, release, candidate, setup_request, workdir,
               max_actions=12, timeout=600, target='local'):
    receipt = {'schemaVersion': 1, 'arm': 'native-cli/terminal-journey-v1', 'attempt': 1,
               'candidateDigest': candidate, 'target': target, 'model': cli.model, 'agent': cli.agent,
               'temperature': None, 'temperaturePolicy': 'CLI default', 'cost': None,
               'metering': 'subscription', 'status': 'infrastructure', 'stages': [],
               'harnessActions': [], 'faultEvents': [], 'approvals': [], 'verification': None}
    workspace = None
    selected = False
    fault_id = 'cli-render-recovery'
    try:
        setup = driver.invoke('setup', setup_request)
        workspace = setup.get('workspaceId')
        if setup.get('status') != 'ready' or not workspace:
            raise ValueError('driver setup was not ready')
        if target != 'local' and setup.get('baseUrl', '').rstrip('/') != target:
            raise ValueError('driver and requested cell origins differ')
        for stage in journey['stages']:
            stage_id = stage['id']
            request = {'workspaceId': workspace, 'stage': stage_id}
            if stage['number'] == 3:
                prepared = driver.invoke('prepare_cli_fixture', {'workspaceId': workspace})
                receipt['harnessActions'].append(prepared)
                if prepared.get('status') != 'pass': raise ValueError('CLI fixture setup failed')
            if stage['number'] == 4:
                armed = driver.invoke('inject_error', {**request, 'errorId': fault_id, 'recoverable': True, 'once': True})
                receipt['harnessActions'].append({'kind': 'arm-fault', **armed})
                if armed.get('status') != 'armed' or armed.get('errorId') != fault_id:
                    raise ValueError('driver did not arm style-render fault')

            def observe_and_approve():
                observed = driver.invoke('observe', request)
                if canary.observed_stage_status(observed, stage) == 'awaiting_approval':
                    proposal = observed.get('proposalId')
                    if not proposal or any(a['proposalId'] == proposal for a in receipt['approvals']):
                        raise ValueError('missing or repeated approval request')
                    approval = driver.invoke('approve', {**request, 'proposalId': proposal,
                                                        'principalProfileReference': 'profile:approver'})
                    if (approval.get('status') != 'approved' or approval.get('proposerSelfApproval') != 'denied'
                            or approval.get('proposalId') != proposal or not approval.get('resolvedBy')
                            or not approval.get('auditId')):
                        raise ValueError('independent approval did not verify')
                    receipt['approvals'].append(approval)
                    observed = driver.invoke('observe', request)
                return observed

            observed = observe_and_approve()
            tool_view = observed.get('toolView')
            if not isinstance(tool_view, dict) or not tool_view.get('tools'):
                raise ValueError('driver omitted server tool surface')
            # Keep the reviewed canary goal and observations; remove the shell bridge.
            payload = json.loads(canary.stage_user_message(stage=stage, candidate_release=release,
                                                           observed=observed, guidance='none'))
            payload.pop('terminalSurface', None)
            payload['task'] = 'Complete this Honua stage using native MCP tools, then answer the customer.'

            def execute(kind, action):
                nonlocal selected
                selected = True
                result = driver.invoke('execute', {**request, 'action': action})
                for name in ('injectedError', 'recoveredError'):
                    fact = canary.fault_evidence(result, name)
                    if isinstance(fact, dict) and fact.get('id') == fault_id:
                        receipt['faultEvents'].append({'kind': name, 'stage': stage_id, **fact})
                # Approval is performed only following a controller observation.
                observe_and_approve()
                return result

            try:
                output = cli.run(prompt=json.dumps(payload, separators=(',', ':')), tool_view=tool_view,
                                 execute=execute, log_dir=Path(workdir) / stage_id,
                                 session=stage['number'], max_actions=max_actions,
                                 max_turns=max_actions + 2, timeout=timeout)
            except CliFailure as exc:
                receipt['stages'].append({'id': stage_id, 'status': 'fail',
                    'actions': [{k:v for k,v in a.items() if k != 'logPath'} for a in exc.progress['actions']],
                    'cliSessions': exc.progress['provenance']})
                raise
            after = observe_and_approve()
            passed = bool(output['actions']) and canary.observed_stage_status(after, stage) == 'complete'
            receipt['stages'].append({'id': stage_id, 'status': 'pass' if passed else 'fail',
                'actions': [{k:v for k,v in a.items() if k != 'logPath'} for a in output['actions']],
                'cliSessions': output['provenance'], 'observationSha256': digest(json.dumps(after).encode()),
                'finalAnswerSha256': digest(output['finalAnswer'].encode())})
            if not passed: raise ValueError('independent stage verification failed')
        fired = any(e['kind'] == 'injectedError' and e.get('recoverable') is True for e in receipt['faultEvents'])
        recovered = any(e['kind'] == 'recoveredError' and e.get('recovered') is True for e in receipt['faultEvents'])
        if not fired or not recovered or not receipt['approvals']:
            raise ValueError('fault recovery or independent approval evidence missing')
        verification = driver.invoke('verify', {'workspaceId': workspace})
        receipt['verification'] = verification
        if (verification.get('status') != 'pass' or not verification.get('finalUrlProof')
                or not verification.get('pixelProof') or not verification.get('canonicalIds')
                or any(verification.get('assertions', {}).get(a) != 'pass' for a in canary.ASSERTION_NAMES)):
            raise ValueError('independent final verification failed')
        receipt['status'] = 'pass'
    except Exception as exc:
        receipt['status'] = 'fail' if selected or isinstance(exc, CliFailure) and exc.category == 'model' else 'infrastructure'
        receipt['failure'] = {'category': type(exc).__name__}  # never export upstream credentials/errors
    finally:
        if workspace:
            try:
                cleanup = driver.invoke('teardown', {'workspaceId': workspace})
                receipt['teardown'] = cleanup.get('status')
                if cleanup.get('status') not in ('pass', 'complete'): receipt['status'] = 'infrastructure'
            except Exception:
                receipt['teardown'] = 'fail'
                receipt['status'] = 'infrastructure'
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=('claude-cli', 'codex-cli'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--target', type=target_url, default='local')
    parser.add_argument('--target-config', type=Path)
    parser.add_argument('--deployment-receipt', type=Path)
    parser.add_argument('--image-ref')
    parser.add_argument('--lock-digest')
    parser.add_argument('--lock-path', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--max-actions-per-stage', type=int, default=12)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    if args.target == 'local': parser.error('CLI arms require a remote VM tunnel or an admitted AWS cell')
    if args.output.exists(): parser.error('receipt exists; never overwrite an earlier attempt')
    candidate, release, image = corpus._candidate_binding(args.image_ref, args.lock_digest, args.lock_path)
    protocol = json.loads(canary.DEFAULT_PROTOCOL.read_bytes())
    protocol['operations']['prepare_cli_fixture'] = {'requiredResponse': ['status', 'attribution']}
    driver = canary.DriverAdapter(corpus.ROOT / 'tools/terminal_model_cli_driver.py', protocol, timeout_seconds=1800)
    setup = {}
    if args.target != 'local':
        if not args.target_config or not args.deployment_receipt:
            parser.error('remote cell needs --target-config and --deployment-receipt from its existing provisioning run')
        config = json.loads(args.target_config.read_bytes())
        if config.get('kind') != 'aws-ecs' or config.get('replicaBaseUrl', '').rstrip('/') != args.target:
            parser.error('target config does not bind this AWS ECS origin')
        proof = args.deployment_receipt
        setup = {'target': str(args.target_config.resolve()), 'deploymentReceipt': {
            'path': str(proof.resolve()), 'sha256': digest(proof.read_bytes()).split(':')[1]}}
    with tempfile.TemporaryDirectory(prefix='honua-cli-journey-') as directory:
        receipt = run_canary(driver=driver, cli=CliModelSession(args.agent, args.model),
                             journey=json.loads(canary.DEFAULT_JOURNEY.read_bytes()), release=release,
                             candidate=candidate, setup_request=setup, workdir=directory,
                             max_actions=args.max_actions_per_stage, timeout=args.timeout, target=args.target)
    receipt.update(serverImage=image, lockDigest=args.lock_digest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + '\n')
    print('CLI terminal canary: ' + receipt['status'])
    return 0 if receipt['status'] == 'pass' else 1


if __name__ == '__main__': raise SystemExit(main())
