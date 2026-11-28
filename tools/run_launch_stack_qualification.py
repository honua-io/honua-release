#!/usr/bin/env python3
"""Fail-closed driver for disposable Redis-off ECS Launch Stack qualification."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import time
from typing import Any, Callable
from urllib.parse import urlsplit

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from emit_live_promise_receipts import DIGEST_RE, RC_IMAGE_SIZE_BUDGET, SHA_RE, STACK_RE
from image_platforms import image_platform_digests, registry_image_platform_digests
from launch_stack_aws import (
    AwsCliAdapter, AwsCommandError, extract_frontend_origin,
    verify_hardened_profile, verify_redis_off,
)
from launch_stack_https import HttpsObservationError, observe_https
from launch_stack_observations import ObservationError, one, require, resource_summaries
from native_ecs_attempt import OwnedAttempt, StackAttempt, describe_with_retry, open_attempt
from native_ecs_lifecycle import cleanup_stack, qualify_installation

DEFAULT_REGION = 'us-east-1'
DEFAULT_SERVER_IMAGE_REPO = 'ghcr.io/honua-io/honua-server'
DEFAULT_ADMIN_EMAIL = 'launch-stack-gate@honua.io'
MAX_STACK_NAME_LENGTH = 128
HTTP_TIMEOUT_SECONDS = 20
CONSOLE_READY_ATTEMPTS = 20
CONSOLE_READY_POLL_SECONDS = 15
IMAGE_CMD_TIMEOUT_SECONDS = 180
DEFAULT_IMAGE_PROFILE = 'postgis-only'
HARDENED_PROFILE = 'Hardened'
ECS_ARCH_TO_OCI = {'X86_64': 'amd64', 'ARM64': 'arm64'}

ROLE_ARN_RE = re.compile(r'^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9+=,.@/_-]+$')
TEMPLATE_URL_RE = re.compile(r'^https://[^\s\x00-\x1f]+$')
ADMIN_EMAIL_RE = re.compile(r'^[^\s@]+@[^\s@]+\.[^\s@]+$')
CONSOLE_VERSION_RE = re.compile(r'^[A-Za-z0-9._-]{1,128}$')
IMAGE_REPO_RE = re.compile(r'^[a-z0-9][a-z0-9._/-]*[a-z0-9]$')


@dataclass(frozen=True)
class QualificationConfig:
    server_sha: str
    server_digest: str
    platform_lock_digest: str
    stack_name: str
    template_url: str
    cfn_role_arn: str
    attempt_id: str
    journal_path: Path
    output_dir: Path
    region: str = DEFAULT_REGION
    image_repo: str = DEFAULT_SERVER_IMAGE_REPO
    console_bundle_version: str = ''
    admin_email: str = DEFAULT_ADMIN_EMAIL


@dataclass(frozen=True)
class ImageMeasurement:
    image_ref: str
    index_digest: str
    platform_digests: dict[str, str]
    platform_sizes: dict[str, int]
    published_aot: bool
    default_image: str


@dataclass(frozen=True)
class QualificationDeps:
    aws: Any
    inspect_image: Callable[[QualificationConfig], ImageMeasurement]
    probe_https: Callable[[str], dict]
    clock: Callable[[], float]
    utc_now: Callable[[], dt.datetime]
    sleep: Callable[[float], None] = time.sleep


def validate_candidate_identity(config: Any) -> str:
    require(SHA_RE.fullmatch(config.server_sha), 'server_sha must be 40 lowercase hex characters')
    require(DIGEST_RE.fullmatch(config.server_digest), 'server_digest must be a sha256 digest')
    require(DIGEST_RE.fullmatch(config.platform_lock_digest),
            'platform_lock_digest must be a sha256 digest')
    require(IMAGE_REPO_RE.fullmatch(config.image_repo), 'image_repo must be a valid registry coordinate')
    return f'{config.image_repo}@{config.server_digest}'


def validate_config(config: QualificationConfig) -> str:
    image_ref = validate_candidate_identity(config)
    require(STACK_RE.fullmatch(config.stack_name) and len(config.stack_name) <= MAX_STACK_NAME_LENGTH,
            'stack_name must match ^honua-gate-[a-zA-Z0-9-]+$ within 128 characters')
    require(TEMPLATE_URL_RE.fullmatch(config.template_url), 'template_url must be a valid https URL')
    require(ROLE_ARN_RE.fullmatch(config.cfn_role_arn), 'HONUA_GATE_CFN_ROLE_ARN must be an IAM role ARN')
    require(ADMIN_EMAIL_RE.fullmatch(config.admin_email), 'admin_email must be a valid email address')
    if config.console_bundle_version:
        require(config.console_bundle_version.lower() != 'off'
                and CONSOLE_VERSION_RE.fullmatch(config.console_bundle_version),
                'console_bundle_version must be a non-off version token')
    return image_ref


def verify_image_measurement(config: QualificationConfig, measurement: ImageMeasurement) -> None:
    expected_ref = f'{config.image_repo}@{config.server_digest}'
    require(measurement.image_ref == expected_ref, 'Measured image reference differs from pinned candidate')
    require(measurement.index_digest == config.server_digest, 'Measured image digest differs from candidate')
    accepted = image_platform_digests(measurement.platform_digests, index_digest=config.server_digest)
    sizes = measurement.platform_sizes
    require(isinstance(sizes, dict) and set(sizes) == set(accepted),
            'Published AOT image size must be measured for every runnable platform variant')
    for arch in sorted(accepted):
        size = sizes[arch]
        require(type(size) is int and 0 < size <= RC_IMAGE_SIZE_BUDGET,
                f'Published AOT {arch} image size is invalid or exceeds the 370 MB RC budget')
    require(measurement.published_aot is True, 'Candidate image does not declare native-aot compilation')
    require(measurement.default_image == DEFAULT_IMAGE_PROFILE,
            'Candidate image does not declare postgis-only default profile')


def _run_docker_command(args: list[str], runner: Callable[..., Any] = subprocess.run) -> str:
    result = runner(
        ['docker', *args], capture_output=True, text=True,
        timeout=IMAGE_CMD_TIMEOUT_SECONDS, check=False,
    )
    require(result.returncode == 0, f'docker {args[0]} failed with exit code {result.returncode}')
    return result.stdout


def _inspect_platform_variant(
    config: QualificationConfig, variant: tuple[str, str], runner: Callable[..., Any],
) -> int:
    arch, child_digest = variant
    child_ref = f'{config.image_repo}@{child_digest}'
    _run_docker_command(['pull', '--quiet', '--platform', f'linux/{arch}', child_ref], runner)
    raw = json.loads(_run_docker_command(['image', 'inspect', child_ref], runner))
    image_doc = one(raw, f'Expected one inspected candidate image for linux/{arch}')
    require(child_ref in (image_doc.get('RepoDigests') or []),
            f'Pulled linux/{arch} image RepoDigests do not confirm immutable child digest pin')
    labels = (image_doc.get('Config') or {}).get('Labels') or {}
    require(labels.get('org.opencontainers.image.revision') == config.server_sha,
            f'Pulled linux/{arch} image source revision label does not match server_sha')
    require(labels.get('honua.runtime.compilation') == 'native-aot',
            f'Pulled linux/{arch} image is not compiled as native-aot')
    require(labels.get('honua.store.profile') == 'postgis',
            f'Pulled linux/{arch} image store profile is not postgis')
    size = image_doc.get('Size')
    require(type(size) is int and 0 < size <= RC_IMAGE_SIZE_BUDGET,
            f'Pulled linux/{arch} AOT image size exceeds RC budget or is invalid')
    return size


def inspect_ghcr_image(
    config: QualificationConfig,
    inspector: Callable[[str, str], dict[str, str]] = registry_image_platform_digests,
    runner: Callable[..., Any] = subprocess.run,
) -> ImageMeasurement:
    image_ref = f'{config.image_repo}@{config.server_digest}'
    platform_digests = inspector(config.image_repo, config.server_digest)
    platform_sizes = {
        arch: _inspect_platform_variant(config, (arch, digest), runner)
        for arch, digest in sorted(platform_digests.items())
    }
    return ImageMeasurement(
        image_ref=image_ref, index_digest=config.server_digest,
        platform_digests=platform_digests, platform_sizes=platform_sizes,
        published_aot=True, default_image=DEFAULT_IMAGE_PROFILE,
    )


class _StrictHttpsTransport:
    """TLS-verifying, no-redirect HTTPS transport for launch_stack_https.observe_https."""

    def __call__(self, url: str, **options: Any) -> dict:
        require(options.get('follow_redirects') is False, 'HTTPS probes must disable redirects')
        parsed = urlsplit(url)
        context = ssl.create_default_context()
        connection = http.client.HTTPSConnection(
            parsed.hostname or '', parsed.port or 443,
            timeout=HTTP_TIMEOUT_SECONDS, context=context,
        )
        try:
            connection.request('GET', parsed.path or '/', headers={'Host': parsed.netloc})
            response = connection.getresponse()
            response.read()
            return {
                'url': url, 'status': response.status,
                'redirected': 300 <= response.status < 400,
                'headers': list(response.headers.items()),
            }
        finally:
            connection.close()


def _try_probe_https(origin: str, deps: QualificationDeps) -> dict | None:
    try:
        return deps.probe_https(origin)
    except (HttpsObservationError, OSError, http.client.HTTPException):
        return None


def _probe_console_ready(origin: str, deps: QualificationDeps) -> dict:
    for attempt in range(1, CONSOLE_READY_ATTEMPTS + 1):
        result = _try_probe_https(origin, deps)
        if result is not None:
            return result
        if attempt < CONSOLE_READY_ATTEMPTS:
            deps.sleep(CONSOLE_READY_POLL_SECONDS)
    raise ObservationError('Console and readiness HTTPS endpoints did not become ready with hardened HSTS')


def _verify_launch_stack_journey(stack_id: str, started: float, deps: QualificationDeps) -> float:
    response = describe_with_retry(deps.aws, stack_id)
    stack = one(response.get('Stacks'), 'Missing or ambiguous qualified stack')
    summaries = resource_summaries(deps.aws, stack_id)
    verify_redis_off(deps.aws, summaries)
    verify_hardened_profile(deps.aws, stack, summaries)
    _probe_console_ready(extract_frontend_origin(stack), deps)
    elapsed_seconds = deps.clock() - started
    require(elapsed_seconds >= 0, 'Clock moved backwards during Launch Stack installation')
    return round(elapsed_seconds / 60.0, 2)


def _deployed_variant_size(measurement: ImageMeasurement, cpu_architecture: str) -> int:
    arch = ECS_ARCH_TO_OCI.get(cpu_architecture)
    require(arch in measurement.platform_sizes, 'Deployed ECS architecture lacks measured image size')
    return measurement.platform_sizes[arch]


def _build_proof(
    config: QualificationConfig, measurement: ImageMeasurement, outcome: dict,
) -> dict:
    installation = outcome['installation']
    cleanup = outcome['cleanup']
    require(cleanup.get('stackStatus') == 'DELETE_COMPLETE' and cleanup.get('remainingResources') == [],
            'Stack teardown did not confirm DELETE_COMPLETE with zero remaining resources')
    return {
        'serverSha': config.server_sha,
        'imageDigest': config.server_digest,
        'platformLockDigest': config.platform_lock_digest,
        'observedAt': outcome['observedAt'],
        'stackName': config.stack_name,
        'orchestrator': 'ecs',
        'redis': False,
        'defaultImage': measurement.default_image,
        'protectionProfile': HARDENED_PROFILE,
        'publishedAotImage': measurement.published_aot,
        'imageSizeBytes': _deployed_variant_size(measurement, installation['cpuArchitecture']),
        'installConsoleMinutes': outcome['installConsoleMinutes'],
        'cpuArchitecture': installation['cpuArchitecture'],
        'resolvedPlatformImageDigest': installation['resolvedPlatformImageDigest'],
        'healthyTaskCount': installation['healthyTaskCount'],
        'cleanup': {
            'stackStatus': cleanup['stackStatus'],
            'remainingResources': cleanup['remainingResources'],
        },
    }


def run_qualification(config: QualificationConfig, deps: QualificationDeps) -> dict:
    image_ref = validate_config(config)
    measurement = deps.inspect_image(config)
    verify_image_measurement(config, measurement)
    account, region = deps.aws.scope()
    require(region == config.region, 'AWS adapter region differs from configured qualification region')
    attempt = StackAttempt(config.journal_path, config.stack_name, account, region, config.attempt_id)
    candidate = {
        'serverImage': image_ref,
        'imageDigest': config.server_digest,
        'platformDigests': measurement.platform_digests,
    }
    started = deps.clock()
    timings: list[float] = []
    result = qualify_installation(
        deps.aws, candidate, attempt=attempt,
        run_journey=lambda stack_id: timings.append(_verify_launch_stack_journey(stack_id, started, deps)),
    )
    require(len(timings) == 1, 'Launch Stack readiness journey did not complete')
    observed_at = deps.utc_now().replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    proof = _build_proof(config, measurement, {
        **result, 'observedAt': observed_at, 'installConsoleMinutes': timings[0],
    })
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / 'launch-stack-proof.json').write_text(
        json.dumps(proof, indent=2) + '\n', encoding='utf-8',
    )
    return proof


def _teardown_or_not_created(owned: OwnedAttempt, deps: QualificationDeps) -> dict:
    retained = owned.retained_arn()
    try:
        stack = owned.observe(deps.aws, stack_id=retained)
        cleanup = cleanup_stack(deps.aws, owned, stack)
        require(cleanup.get('stackStatus') == 'DELETE_COMPLETE' and cleanup.get('remainingResources') == [],
                'Stack teardown did not confirm DELETE_COMPLETE with zero remaining resources')
        return cleanup
    except AwsCommandError as error:
        if retained is None and error.error_code == 'ValidationError':
            return {'stackStatus': 'NOT_CREATED', 'remainingResources': []}
        raise


def cleanup_if_started(config: QualificationConfig, deps: QualificationDeps) -> dict | None:
    image_ref = validate_config(config)
    creating = config.journal_path.with_suffix(f'{config.journal_path.suffix}.creating')
    if not config.journal_path.exists() or not creating.exists():
        return None
    account, region = deps.aws.scope()
    require(region == config.region, 'AWS adapter region differs from configured qualification region')
    attempt = StackAttempt(config.journal_path, config.stack_name, account, region, config.attempt_id)
    with open_attempt(attempt, image_ref) as owned:
        return _teardown_or_not_created(owned, deps)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-sha', required=True)
    parser.add_argument('--server-digest', required=True)
    parser.add_argument('--platform-lock-digest', required=True)
    parser.add_argument('--stack-name', required=True)
    parser.add_argument('--attempt-id', required=True)
    parser.add_argument('--journal', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--region', default=DEFAULT_REGION)
    parser.add_argument('--image-repo', default=DEFAULT_SERVER_IMAGE_REPO)
    parser.add_argument('--console-bundle-version', default='')
    parser.add_argument('--cleanup-only', action='store_true')
    return parser.parse_args(argv)


def _config_from_env(args: argparse.Namespace) -> QualificationConfig:
    return QualificationConfig(
        server_sha=args.server_sha, server_digest=args.server_digest,
        platform_lock_digest=args.platform_lock_digest, stack_name=args.stack_name,
        template_url=os.environ.get('HONUA_GATE_TEMPLATE_URL', ''),
        cfn_role_arn=os.environ.get('HONUA_GATE_CFN_ROLE_ARN', ''),
        attempt_id=args.attempt_id, journal_path=args.journal, output_dir=args.out,
        region=args.region, image_repo=args.image_repo,
        console_bundle_version=args.console_bundle_version,
        admin_email=os.environ.get('HONUA_GATE_ADMIN_EMAIL', '') or DEFAULT_ADMIN_EMAIL,
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = _config_from_env(args)
        transport = _StrictHttpsTransport()
        deps = QualificationDeps(
            aws=AwsCliAdapter(config),
            inspect_image=inspect_ghcr_image,
            probe_https=lambda origin: observe_https(transport, origin),
            clock=time.monotonic,
            utc_now=lambda: dt.datetime.now(dt.timezone.utc),
            sleep=time.sleep,
        )
        if args.cleanup_only:
            cleanup_if_started(config, deps)
        else:
            run_qualification(config, deps)
    except (ObservationError, HttpsObservationError, AwsCommandError, ValueError, OSError) as error:
        print(f'Launch Stack qualification failed: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
