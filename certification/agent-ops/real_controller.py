"""Narrow live Compose action adapter; it does not authorize a priced trial.

A complete runner must seed and observe the real customer corpus, provision a
separate agent identity, capture all attempted actions, and grade independent
readback before it can use these actions as evaluation evidence.
"""
from __future__ import annotations

import time
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ga_actions import _PROJECT, invoke
from local_http import open_local, require_loopback


def _contains_secret_like_text(text: str) -> bool:
    return bool(re.search(r'(?i)(?:password|token|secret|authorization|api[-_]?key)\s*[:=]\s*\S+', text) or
                re.search(r'(?i)https?://[^\s/@:]+:[^\s/@]+@', text))


@dataclass
class RealController:
    project: str
    compose_file: Path
    output: Path
    prior_image: str
    candidate_image: str
    base_url: str
    backup_sha256: str | None = None
    phase: str = "prior"
    execute: Callable = invoke
    audit: list[dict] = field(default_factory=list)

    def _invoke(self, operation: str) -> dict:
        return self.execute(operation=operation, project=self.project, compose_file=self.compose_file,
                            output=self.output, prior_image=self.prior_image,
                            candidate_image=self.candidate_image, base_url=self.base_url,
                            expected_backup_sha256=self.backup_sha256)

    def _diagnose(self, *, logs: bool) -> dict:
        if not _PROJECT.fullmatch(self.project) or not self.compose_file.is_file():
            raise ValueError('invalid trial project')
        require_loopback(self.base_url)
        if logs:
            from client import Redactor
            result = subprocess.run(['docker', 'compose', '-p', self.project, '-f',
                                     str(self.compose_file.resolve()), 'logs', '--no-color', '--tail', '200', 'server'],
                                    capture_output=True, text=True, timeout=15, check=False)
            if result.returncode:
                return {'ok': False, 'error': 'logs-unavailable'}
            secrets = [os.environ.get(name, '') for name in (
                'E2E_API_KEY', 'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY', 'TERMINAL_MODEL_API_KEY',
                'HONUA_CANARY_AWS_ACCESS_KEY_ID', 'HONUA_CANARY_AWS_SECRET_ACCESS_KEY',
                'HONUA_CANARY_AWS_SESSION_TOKEN', 'HONUA_CANARY_TRANSCRIPT_SIGNING_SEED',
            )]
            text = Redactor(sensitive_values=secrets).text(result.stdout)[-16000:]
            if _contains_secret_like_text(text):
                return {'ok': False, 'error': 'logs-sensitive'}
            return {'ok': True, 'log': text, 'empty': not bool(text),
                    'log_sha256': hashlib.sha256(text.encode()).hexdigest()}
        prefix = ['docker', 'compose', '-p', self.project, '-f', str(self.compose_file.resolve())]
        ps = subprocess.run(prefix + ['ps', '-q', '--status', 'running', 'server'],
                            capture_output=True, text=True, timeout=15, check=False)
        containers = ps.stdout.split()
        if ps.returncode or not containers or any(not re.fullmatch(r'[a-f0-9]{12,64}', cid) for cid in containers):
            return {'ok': False, 'ready': False}
        for container in containers:
            check = subprocess.run(['docker', 'exec', container, 'wget', '-q', '-T', '5', '-O', '/dev/null',
                                    'http://127.0.0.1:8080/healthz/ready'],
                                   capture_output=True, text=True, timeout=10, check=False)
            if check.returncode:
                return {'ok': False, 'ready': False}
        return {'ok': True, 'ready': True, 'replicas_checked': len(containers),
                'scope': 'currently-running-replicas'}

    def _metrics(self) -> dict:
        from client import Redactor
        from real_seed import _verify_runtime
        require_loopback(self.base_url)
        ref = self.prior_image if self.phase == 'prior' else self.candidate_image if self.phase == 'upgraded' else None
        if ref is None or '@sha256:' not in ref:
            return {'ok': False, 'error': 'runtime-identity-unavailable'}
        try:
            _verify_runtime(self.project, self.compose_file, ref, ref.split('@', 1)[1], self.base_url)
        except ValueError:
            return {'ok': False, 'error': 'runtime-identity-unavailable'}
        try:
            with open_local(urllib.request.Request(self.base_url.rstrip('/') + '/metrics'), timeout=5) as response:
                raw = response.read(65537)
                status = response.status
        except (OSError, ValueError):
            return {'ok': False, 'error': 'metrics-unavailable'}
        if status != 200 or len(raw) > 65536:
            return {'ok': False, 'error': 'metrics-unavailable'}
        secrets = [os.environ.get(name, '') for name in (
            'E2E_API_KEY', 'HONUA_ADMIN_PASSWORD', 'HONUA_MASTER_KEY', 'TERMINAL_MODEL_API_KEY',
            'HONUA_CANARY_AWS_ACCESS_KEY_ID', 'HONUA_CANARY_AWS_SECRET_ACCESS_KEY',
            'HONUA_CANARY_AWS_SESSION_TOKEN', 'HONUA_CANARY_TRANSCRIPT_SIGNING_SEED',
        )]
        text = Redactor(sensitive_values=secrets).text(raw.decode('utf-8', 'replace'))
        if _contains_secret_like_text(text):
            return {'ok': False, 'error': 'metrics-sensitive'}
        return {'ok': True, 'metrics': text, 'metrics_sha256': hashlib.sha256(text.encode()).hexdigest()}

    def apply(self, tool: str, arguments: dict) -> dict:
        started = time.time()
        known = {'honua_platform_release_status', 'honua_deploy_operations'}
        tool_name = tool if isinstance(tool, str) and tool in known else 'unknown-tool'
        tool_digest = hashlib.sha256(str(tool).encode()).hexdigest()
        event = {"tool": tool_name, "tool_sha256": tool_digest, "arguments": None,
                 "arguments_sha256": None, "started_at": started,
                 "effect": "read", "ok": False}
        try:
            encoded = json.dumps(arguments, sort_keys=True, separators=(',', ':'), default=str).encode()
            event['arguments_sha256'] = hashlib.sha256(encoded).hexdigest()
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            if tool == "honua_platform_release_status" and not arguments:
                result = self._invoke("schema")
            elif tool == "honua_deploy_operations":
                operation = arguments.get("operation")
                if operation == "backup" and set(arguments) == {"operation"}:
                    event["effect"] = "mutation"
                    if self.phase != "prior" or self.backup_sha256:
                        result = {"ok": False, "error": "backup-order"}
                    else:
                        result = self._invoke("backup")
                        if result.get("ok") is True:
                            self.backup_sha256 = result["backup_sha256"]
                elif operation == "apply" and set(arguments) <= {"operation", "image"} and arguments.get("image") == self.candidate_image:
                    event["effect"] = "mutation"
                    if self.phase != "prior" or not self.backup_sha256:
                        result = {"ok": False, "error": "pre-upgrade-backup-required"}
                    else:
                        # A failed or interrupted migration may have changed the DB.
                        self.phase = "unknown"
                        result = self._invoke("start-candidate")
                        if result.get("ok") is True:
                            self.phase = "upgraded"
                elif operation == "restore_backup" and set(arguments) <= {"operation", "backup_id"} and arguments.get("backup_id") == "previous-release":
                    event["effect"] = "mutation"
                    if not self.backup_sha256:
                        result = {"ok": False, "error": "backup-unavailable"}
                    elif self.phase not in {"upgraded", "unknown"}:
                        result = {"ok": False, "error": "restore-order"}
                    else:
                        self.phase = "unknown"
                        result = self._invoke("restore-prior")
                        if result.get("ok") is True:
                            self.phase = "prior"
                elif operation in {"rollback", "downgrade", "apply"} and arguments.get("image") == self.prior_image:
                    # Record the attempt even though this adapter refuses it.
                    event["effect"] = "image_downgrade"
                    result = {"ok": False, "error": "restore-required"}
                else:
                    result = {"ok": False, "error": "unsupported-operation"}
            else:
                result = {"ok": False, "error": "not-real"}
            event["ok"] = result.get("ok") is True
            event["receipt"] = {key: result[key] for key in (
                "ok", "error", "operation", "project", "gate_sha256", "compose_sha256",
                "exit_code", "backup_sha256", "runtime_image_ids", "schema",
                "ready", "http_status", "log_sha256", "metrics_sha256",
            ) if key in result}
            return result
        except Exception as exc:
            event["error_type"] = type(exc).__name__
            raise
        finally:
            event["finished_at"] = time.time()
            self.audit.append(event)
