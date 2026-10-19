#!/usr/bin/env python3
"""Authenticate a retained controller campaign before evaluating its receipts.

The expected campaign digest is a pre-execution, operator-frozen input. The
controller producer is intentionally required even while not yet provisioned;
diagnostic/preflight workflows cannot substitute for this producer.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

import agent_e2e_gate as gate

REPOSITORY = 'honua-io/honua-release'
WORKFLOW = '.github/workflows/agent-e2e-campaign.yml'


def call(arguments):
    return json.loads(subprocess.check_output(arguments, text=True, encoding='utf-8'))


def authenticate(path, source_sha, run_id, run_attempt, *, invoke=call):
    result = invoke(['gh', 'attestation', 'verify', str(path), '--repo', REPOSITORY,
                     '--cert-identity', f'https://github.com/{REPOSITORY}/{WORKFLOW}@refs/heads/trunk',
                     '--source-digest', source_sha, '--source-ref', 'refs/heads/trunk',
                     '--cert-oidc-issuer', 'https://token.actions.githubusercontent.com',
                     '--deny-self-hosted-runners', '--predicate-type', 'https://slsa.dev/provenance/v1',
                     '--format', 'json'])
    gate.require(isinstance(result, list) and result, 'no cryptographically verified attestation')
    expected_run = f'https://github.com/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}'
    for entry in result:
        verified = entry['verificationResult']
        statement = verified['statement']
        cert = verified['signature']['certificate']
        gate.require([s['digest'].get('sha256') for s in statement['subject']] ==
                     [gate.digest(path.read_bytes()).removeprefix('sha256:')], 'wrong attestation subject')
        gate.require(cert['runInvocationURI'] == expected_run, 'attestation belongs to different run/attempt')
        gate.require(cert['sourceRepositoryDigest'] == source_sha and
                     cert['sourceRepositoryURI'] == f'https://github.com/{REPOSITORY}' and
                     cert['runnerEnvironment'] == 'github-hosted' and
                     cert['buildSignerURI'] == f'https://github.com/{REPOSITORY}/{WORKFLOW}@refs/heads/trunk',
                     'attestation producer identity mismatch')


def verify(bundle: Path, pack: Path, current_lock: Path, campaign_digest: str,
           source_sha: str, run_id: int, run_attempt: int, *, invoke=call):
    gate.require(re.fullmatch(r'[0-9a-f]{40}', source_sha) is not None, 'exact controller source required')
    gate.require(type(run_id) is int and run_id > 0 and type(run_attempt) is int and run_attempt > 0,
                 'exact producer run and attempt required')
    metadata = invoke(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{run_attempt}'])
    gate.require(metadata['id'] == run_id and metadata['run_attempt'] == run_attempt and
                 metadata['head_sha'] == source_sha and metadata['head_branch'] == 'trunk' and
                 metadata['path'] == WORKFLOW and metadata['status'] == 'completed' and
                 metadata['conclusion'] == 'success' and metadata['event'] == 'workflow_dispatch',
                 'controller is not the exact successful trusted trunk campaign')
    campaign_path = bundle / 'campaign.json'
    inventory_path = bundle / 'trusted-inventory.json'
    gate.require(gate.digest(campaign_path.read_bytes()) == campaign_digest,
                 'campaign differs from pre-execution frozen digest')
    authenticate(campaign_path, source_sha, run_id, run_attempt, invoke=invoke)
    authenticate(inventory_path, source_sha, run_id, run_attempt, invoke=invoke)
    campaign = gate.read_json(campaign_path)
    expected_controller = {'repository': REPOSITORY, 'workflow': WORKFLOW, 'sourceSha': source_sha,
                           'runId': run_id, 'runAttempt': run_attempt}
    gate.require(campaign['controller'] == expected_controller, 'campaign controller identity differs from attestation')
    gate.require(gate.component_binding(gate.read_json(current_lock)) == campaign['componentBindingDigest'],
                 'campaign does not qualify this frozen candidate')
    result = gate.validate(pack, campaign_path, bundle / 'qualification-lock.json', inventory_path,
                           bundle / 'receipts', bundle / 'evidence', bundle / 'budget-ledger.json',
                           bundle / 'expectations.json', bundle / 'attempt-registry.json')
    result.update(qualificationLockDigest=campaign['qualificationLockDigest'],
                  corpusDigest=campaign['corpusDigest'], campaignDigest=campaign_digest,
                  inventoryDigest=gate.digest(inventory_path.read_bytes()),
                  controller=expected_controller, completedAt=metadata['updated_at'], expiresAt=campaign['expiresAt'])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('bundle', 'pack', 'current-lock', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('campaign-digest', 'source-sha'):
        parser.add_argument('--' + name, required=True)
    for name in ('run-id', 'run-attempt'):
        parser.add_argument('--' + name, type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = verify(args.bundle, args.pack, args.current_lock, args.campaign_digest,
                        args.source_sha, args.run_id, args.run_attempt)
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as exc:
        result = {'schemaVersion': 1, 'status': 'fail', 'errors': [str(exc)]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    return 0 if result['status'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
