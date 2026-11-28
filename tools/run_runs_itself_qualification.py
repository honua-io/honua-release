#!/usr/bin/env python3
"""Fail-closed driver for AWS Runs-Itself live fault qualification proof production."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from emit_live_promise_receipts import OPERATES_IDS
from launch_stack_aws import AwsCommandError
from launch_stack_observations import ObservationError, require
from run_launch_stack_qualification import (
    DEFAULT_SERVER_IMAGE_REPO, inspect_ghcr_image, validate_candidate_identity, verify_image_measurement,
)
from runs_itself_aws import (
    MetricPendingError, make_aws_client, restore_stack_safety,
    verify_row_notifications, verify_stack_candidate_binding,
)

DEFAULT_REGION = 'us-east-1'
GATE_TIMEOUT_SECONDS = 5400
NOTIFICATION_POLL_ATTEMPTS = 6
NOTIFICATION_POLL_SECONDS = 30
GATE_STACK_RE = re.compile(r'^honua-gate-[1-9][0-9]*$')
REQUIRED_ROWS = (1, 2, 3, 4, 5, 6, 9, 10, 11, 12)
ROW_TO_OPERATES_ID = {
    1: 'operates-1',
    2: 'operates-2',
    3: 'operates-3',
    4: 'operates-4',
    5: 'operates-5',
    6: 'operates-6',
    9: 'operates-9',
    10: 'operates-10',
    11: 'operates-11',
    12: 'operates-12-notify',
}
UNATTENDED_ROWS = frozenset({1, 2, 3, 5, 6})
RESTORED_ROWS = frozenset({2, 3, 4, 5, 6, 9, 10, 11, 12})
ROW12_MESSAGE_ID_KEYS = (
    'initial_sns_message_id', 'initial_sqs_message_id',
    'renotify_sns_message_id', 'renotify_sqs_message_id',
    'expiry_sns_message_id', 'expiry_sqs_message_id',
)


@dataclass(frozen=True)
class RunsItselfConfig:
    server_sha: str
    server_digest: str
    platform_lock_digest: str
    stack_name: str
    gate_script: Path
    artifacts_dir: Path
    output_dir: Path
    budget_stack_name: str = ''
    region: str = DEFAULT_REGION
    image_repo: str = DEFAULT_SERVER_IMAGE_REPO


@dataclass(frozen=True)
class RunsItselfDeps:
    aws: Any
    inspect_image: Callable[[RunsItselfConfig], Any]
    run_gate: Callable[[list[str]], subprocess.CompletedProcess]
    verify_notifications: Callable[[Any, str, tuple[dt.datetime, dt.datetime]], dict]
    utc_now: Callable[[], dt.datetime]
    sleep: Callable[[float], None] = time.sleep


def validate_config(config: RunsItselfConfig) -> None:
    validate_candidate_identity(config)
    require(GATE_STACK_RE.fullmatch(config.stack_name),
            'stack_name must match ^honua-gate-[1-9][0-9]*$')
    if config.budget_stack_name:
        require(GATE_STACK_RE.fullmatch(config.budget_stack_name),
                'budget_stack_name must match ^honua-gate-[1-9][0-9]*$')
        require(config.budget_stack_name != config.stack_name,
                'budget_stack_name must differ from stack_name')
    require(config.region == DEFAULT_REGION, 'Runs-Itself qualification requires us-east-1')


def configured_stacks(config: RunsItselfConfig) -> list[str]:
    stacks = [config.stack_name]
    if config.budget_stack_name:
        stacks.append(config.budget_stack_name)
    return stacks


def target_stack_for_row(config: RunsItselfConfig, row: int) -> str:
    return config.budget_stack_name if row == 3 and config.budget_stack_name else config.stack_name


def _is_positive_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def _has_observation(observations: list[dict], key: str) -> bool:
    return any(isinstance(item, dict) and item.get(key) is True for item in observations)


def _validate_row_specific_evidence(row: int, observations: list[dict]) -> None:
    if row == 1:
        require(_has_observation(observations, 'service_recovered_without_capacity_change'),
                'Row 1 receipt missing service_recovered_without_capacity_change evidence')
    if row in RESTORED_ROWS:
        require(_has_observation(observations, 'fault_restored'),
                f'Row {row} receipt missing fault_restored confirmation')
    if row in UNATTENDED_ROWS:
        require(not _has_observation(observations, 'started_by_harness'),
                f'Unattended row {row} required manual harness recovery')
    if row == 11:
        require(_has_observation(observations, 'notify_only_diagnosed')
                and _has_observation(observations, 'zero_mutations_observed'),
                'Row 11 receipt missing NotifyOnly diagnosis or zero-mutation evidence')
    if row == 12:
        _validate_row12_evidence(observations)


def _validate_row12_evidence(observations: list[dict]) -> None:
    for flag in ('human_needed_notified', 'plain_language_guidance_verified',
                 'zero_mutations_observed', 'expired_safely'):
        require(_has_observation(observations, flag), f'Row 12 receipt missing {flag} evidence')
    matching = [item for item in observations if isinstance(item, dict) and item.get('human_needed_notified') is True]
    detail = matching[-1]
    for key in ROW12_MESSAGE_ID_KEYS:
        value = detail.get(key)
        require(isinstance(value, str) and bool(value.strip()), f'Row 12 receipt missing {key}')


def validate_row_receipt(row: int, receipt: dict) -> float:
    require(isinstance(receipt, dict), f'Row {row} receipt must be a JSON object')
    require(receipt.get('row') == row, f'Row {row} receipt row field mismatch')
    require(receipt.get('executed') is True, f'Row {row} was not executed')
    result = str(receipt.get('result', ''))
    require(result.startswith('OBSERVED_'), f'Row {row} did not reach an OBSERVED_ status: {result}')
    recovery = receipt.get('recovery_minutes')
    require(_is_positive_number(recovery), f'Row {row} receipt missing positive recovery_minutes')
    observations = receipt.get('observations')
    require(isinstance(observations, list) and len(observations) > 0,
            f'Row {row} receipt missing observations')
    _validate_row_specific_evidence(row, observations)
    return float(recovery)


def _load_single_receipt(row: int, stack_name: str, row_dir: Path) -> dict:
    matches = sorted(row_dir.glob(f'row-{row}-{stack_name}-*.json'))
    require(len(matches) == 1, f'Expected exactly one receipt for row {row} on {stack_name}')
    return json.loads(matches[0].read_text(encoding='utf-8'))


def _try_verify_notifications(
    stack_name: str, window: tuple[dt.datetime, dt.datetime], deps: RunsItselfDeps,
) -> dict | None:
    try:
        return deps.verify_notifications(deps.aws, stack_name, window)
    except MetricPendingError:
        return None


def _poll_row_notifications(
    stack_name: str, window: tuple[dt.datetime, dt.datetime], deps: RunsItselfDeps,
) -> dict:
    for attempt in range(1, NOTIFICATION_POLL_ATTEMPTS + 1):
        evidence = _try_verify_notifications(stack_name, window, deps)
        if evidence is not None:
            return evidence
        if attempt < NOTIFICATION_POLL_ATTEMPTS:
            deps.sleep(NOTIFICATION_POLL_SECONDS)
    return deps.verify_notifications(deps.aws, stack_name, window)


def _execute_row(row: int, config: RunsItselfConfig, deps: RunsItselfDeps) -> tuple[dict, dict]:
    stack_name = target_stack_for_row(config, row)
    row_dir = config.artifacts_dir / f'row-{row}'
    row_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, str(config.gate_script), 'run', str(row),
        '--stack', stack_name, '--region', config.region,
        '--execute', '--artifacts', str(row_dir),
    ]
    window_start = deps.utc_now()
    completed = deps.run_gate(command)
    window_end = deps.utc_now()
    require(completed.returncode == 0, f'Gate harness failed on row {row} with exit code {completed.returncode}')
    receipt = _load_single_receipt(row, stack_name, row_dir)
    recovery_minutes = validate_row_receipt(row, receipt)
    evidence = _poll_row_notifications(stack_name, (window_start, window_end), deps)
    require(evidence.get('notificationVerified') is True and evidence.get('inboxVerified') is True,
            f'Row {row} failed programmatic SNS/ops-inbox confirmation')
    entry = {
        'status': 'PASS',
        'recoveryMinutes': recovery_minutes,
        'stackName': stack_name,
        'notificationVerified': True,
        'inboxVerified': True,
    }
    return receipt, entry


def compute_unattended_person_minutes(receipts: dict[int, dict]) -> int | float:
    total = 0.0
    for row in sorted(UNATTENDED_ROWS):
        receipt = receipts.get(row)
        require(isinstance(receipt, dict), f'Missing unattended row {row} receipt')
        minutes = receipt.get('person_minutes', 0)
        require(type(minutes) in (int, float) and math.isfinite(minutes) and minutes >= 0,
                f'Invalid person_minutes recorded on row {row}')
        total += float(minutes)
    return 0 if total == 0.0 else round(total, 2)


def _verify_candidate_stacks(config: RunsItselfConfig, deps: RunsItselfDeps) -> None:
    measurement = deps.inspect_image(config)
    verify_image_measurement(config, measurement)
    candidate = {
        'server_digest': config.server_digest,
        'platform_digests': measurement.platform_digests,
    }
    for stack_name in configured_stacks(config):
        verify_stack_candidate_binding(deps.aws, stack_name, candidate)


def run_qualification(config: RunsItselfConfig, deps: RunsItselfDeps) -> dict:
    validate_config(config)
    require(config.gate_script.is_file(), 'Gate harness script does not exist')
    _verify_candidate_stacks(config, deps)
    receipts: dict[int, dict] = {}
    rows_payload: dict[str, dict] = {}
    for row in REQUIRED_ROWS:
        receipt, entry = _execute_row(row, config, deps)
        receipts[row] = receipt
        rows_payload[ROW_TO_OPERATES_ID[row]] = entry
    require(tuple(rows_payload.keys()) == OPERATES_IDS,
            'Runs-Itself rows payload does not match required OPERATES_IDS')
    observed_at = deps.utc_now().replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    proof = {
        'serverSha': config.server_sha,
        'imageDigest': config.server_digest,
        'platformLockDigest': config.platform_lock_digest,
        'observedAt': observed_at,
        'stackName': config.stack_name,
        'unattendedPersonMinutes': compute_unattended_person_minutes(receipts),
        'rows': rows_payload,
    }
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / 'runs-itself-proof.json').write_text(
        json.dumps(proof, indent=2) + '\n', encoding='utf-8',
    )
    return proof


def cleanup_stacks(config: RunsItselfConfig, aws: Any) -> list[dict]:
    validate_config(config)
    results: list[dict] = []
    errors: list[str] = []
    for name in configured_stacks(config):
        try:
            results.append(restore_stack_safety(aws, name))
        except (ObservationError, AwsCommandError, ValueError, OSError, TimeoutError) as error:
            errors.append(f'{name}: {error}')
    if errors:
        raise ObservationError('Safety restoration failed for one or more stacks: ' + '; '.join(errors))
    return results


def _default_run_gate(command: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=GATE_TIMEOUT_SECONDS, check=False)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--platform-lock-digest', required=True)
    parser.add_argument('--stack-name', required=True)
    parser.add_argument('--budget-stack-name', default='')
    parser.add_argument('--gate-script', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--region', default=DEFAULT_REGION)
    parser.add_argument('--image-repo', default=DEFAULT_SERVER_IMAGE_REPO)
    parser.add_argument('--cleanup-only', action='store_true')
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = RunsItselfConfig(
        server_sha=args.server_sha, server_digest=args.server_digest,
        platform_lock_digest=args.platform_lock_digest, stack_name=args.stack_name,
        budget_stack_name=args.budget_stack_name, gate_script=args.gate_script,
        artifacts_dir=args.artifacts, output_dir=args.out, region=args.region,
        image_repo=args.image_repo,
    )
    try:
        aws = make_aws_client(config.region)
        if args.cleanup_only:
            cleanup_stacks(config, aws)
        else:
            deps = RunsItselfDeps(
                aws=aws, inspect_image=inspect_ghcr_image, run_gate=_default_run_gate,
                verify_notifications=verify_row_notifications,
                utc_now=lambda: dt.datetime.now(dt.timezone.utc),
            )
            run_qualification(config, deps)
    except (ObservationError, AwsCommandError, ValueError, OSError, TimeoutError) as error:
        print(f'Runs-Itself qualification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
