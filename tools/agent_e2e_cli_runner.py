"""Corpus adapter for native CLI sessions; reuses W2 custody and independent scoring."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from urllib.parse import urlsplit

import agent_e2e_local_runner as corpus
from agent_e2e_local_api import Stack
from agent_cli_sessions import CliFailure, CliModelSession, digest


def target_url(value):
    if value == 'local': return value
    parsed = urlsplit(value)
    if (parsed.scheme != 'https' or not re.fullmatch(r'[a-z0-9-]+\.cert\.demo\.honua\.io', parsed.hostname or '')
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.query or parsed.fragment or parsed.path not in ('', '/')):
        raise ValueError('target must be local or an HTTPS *.cert.demo.honua.io cell origin')
    return value.rstrip('/')


class CliRuntime(corpus.LocalRuntime):
    def __init__(self, *args, agent, timeout=600, target='local', target_config=None,
                 deployment_receipt=None, session_factory=CliModelSession, **kwargs):
        super().__init__(*args, agent=agent, **kwargs)
        import agent_e2e_cli_sessions  # registers W2 ModelSession implementations
        self.cli = session_factory(agent, self.model)
        self.timeout, self.target = timeout, target_url(target)
        self.target_config, self.deployment_receipt = target_config, deployment_receipt
        self.cli_records = []
        if target != 'local': self.ADMIN_ENV = 'HONUA_CLOUD_JOURNEY_ADMIN'

    def setup(self, case_id, candidate_digest, needs_approver=False, agent_permissions=('admin:write',)):
        if self.target == 'local':
            raise ValueError('CLI arms require a remote VM tunnel or an admitted AWS cell; host Docker is disabled')
        if needs_approver:
            raise ValueError('corpus separate approver is not implemented by W2 yet')
        if not self.target_config or not self.deployment_receipt:
            raise ValueError('AWS target requires retained target config and deterministic deployment receipt')
        config = json.loads(Path(self.target_config).read_bytes())
        if config.get('kind') != 'aws-ecs' or config.get('baseUrl', '').rstrip('/') != self.target:
            # Some target versions name the endpoint replicaBaseUrl; both must match when present.
            if config.get('kind') != 'aws-ecs' or config.get('replicaBaseUrl', '').rstrip('/') != self.target:
                raise ValueError('reviewed AWS ECS config does not bind requested cell')
        for key in ('baseUrl', 'replicaBaseUrl'):
            if key in config and config[key].rstrip('/') != self.target:
                raise ValueError('target origin mismatch')
        admin = os.environ.get(self.ADMIN_ENV)
        if not admin: raise ValueError('missing controller cell credential')
        case_dir = tempfile.mkdtemp(prefix='honua-cli-corpus-')
        self.workdirs.append(case_dir)
        proof = Path(self.deployment_receipt)
        response = self.driver.invoke('setup', {'target': str(Path(self.target_config).resolve()),
            'deploymentReceipt': {'path': str(proof.resolve()), 'sha256': digest(proof.read_bytes()).split(':')[1]}})
        workspace = response.get('workspaceId')
        if response.get('status') != 'ready' or response.get('baseUrl', '').rstrip('/') != self.target:
            if workspace: self.driver.invoke('teardown', {'workspaceId': workspace})
            raise ValueError('cell driver did not verify fresh candidate deployment')
        stack = Stack(self.target, admin, None, 'cli-' + Path(case_dir).name, '', case_dir,
                      candidate_digest, {'workspaceId': workspace})
        self.verified_remote_origin = self.target  # set only after the deployment receipt passed driver setup
        self._secrets.append(admin)
        try:
            self._prepare(stack, list(agent_permissions))
            # The cell belongs to the release provisioner. No Docker/AWS restart fallback.
            stack.extra['restartServer'] = lambda prefix: (_ for _ in ()).throw(
                ValueError('AWS cell restart requires a real controller operation; unavailable'))
        except Exception:
            self.teardown(stack)
            raise
        return stack

    def teardown(self, stack):
        failed = False
        if self.target != 'local' and 'recorder' in stack.extra:
            recorder = stack.extra['recorder']
            recorder.set_phase('observe')
            for index, (name, (_env, identity)) in enumerate(list(recorder.principals.items())):
                if name in ('controller', 'anonymous') or not identity: continue
                try:
                    answer = recorder.json(f'cli-cleanup-key-{index}', 'POST',
                                           f'/api/v1/admin/api-keys/{identity}/revoke')
                    failed |= answer['observation']['httpStatus'] not in (200, 204, 404)
                except Exception:
                    failed = True
        try:
            super().teardown(stack)  # always clean up the driver's separately minted principals as well
        finally:
            if failed: raise ValueError('remote case credential cleanup failed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--agent', required=True, choices=('claude-cli', 'codex-cli'))
    parser.add_argument('--model', required=True)
    parser.add_argument('--scenario', required=True)
    parser.add_argument('--variant', choices=('A', 'B'), required=True)
    parser.add_argument('--pack', type=Path, default=corpus.PACK)
    parser.add_argument('--image-ref')
    parser.add_argument('--lock-digest')
    parser.add_argument('--lock-path', type=Path)
    parser.add_argument('--target', type=target_url, default='local')
    parser.add_argument('--target-config', type=Path)
    parser.add_argument('--deployment-receipt', type=Path)
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists(): parser.error('receipt already exists; trials may never overwrite earlier attempts')
    # The W2 loader refuses the full pack by name before opening it and verifies A/B bytes.
    case = corpus.select_case(args.pack, args.scenario, args.variant)
    candidate, release, image = corpus._candidate_binding(args.image_ref, args.lock_digest, args.lock_path)
    protocol = json.loads((corpus.ROOT / 'certification/terminal-model-canary/driver-protocol.v1.json').read_bytes())
    driver = corpus._canary().DriverAdapter(corpus.ROOT / protocol['adapterPath'], protocol, timeout_seconds=1800)
    runtime = CliRuntime(driver, agent=args.agent, model=args.model, release_id=release,
                         max_model_calls=case['limits']['maxTurns'], max_actions=case['limits']['maxActions'],
                         timeout=args.timeout, target=args.target, target_config=args.target_config,
                         deployment_receipt=args.deployment_receipt)
    receipt = corpus.execute_case(case_id=f'{args.scenario}:{args.variant}:local-docker',
                                 scenario=corpus.load_scenario(args.scenario), variant=args.variant, case=case,
                                 candidate_digest=candidate, runtime=runtime)
    receipt.update(agent=args.agent, cost=None, metering='subscription',
                   target=args.target, serverImage=image, lockDigest=args.lock_digest,
                   temperaturePolicy='CLI default', cliSessions=runtime.cli_records,
                   modelVisibleScope='runner-controlled prompt, MCP responses, tool list, flags and config')
    if args.target != 'local': receipt['caseId'] = receipt['caseId'].replace('local-docker', 'aws-ecs')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for directory in runtime.workdirs:
        try:
            corpus.export_case_evidence(Path(directory), args.output.parent, runtime._secrets)
        finally:
            shutil.rmtree(directory)
    args.output.write_text(runtime.redact(json.dumps(receipt, indent=2)) + '\n')
    print(f"{args.scenario}:{args.variant}: {receipt['status']}")
    return 0 if receipt['status'] == 'pass' else 1


if __name__ == '__main__': raise SystemExit(main())
