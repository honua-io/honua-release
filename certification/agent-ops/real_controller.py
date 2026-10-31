"""Narrow live Compose action adapter; it does not authorize a priced trial.

A complete runner must seed and observe the real customer corpus, provision a
separate agent identity, capture all attempted actions, and grade independent
readback before it can use these actions as evaluation evidence.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ga_actions import invoke


@dataclass
class RealController:
    project: str
    compose_file: Path
    output: Path
    prior_image: str
    candidate_image: str
    base_url: str
    backup_sha256: str | None = None
    execute: Callable = invoke
    audit: list[dict] = field(default_factory=list)

    def _invoke(self, operation: str) -> dict:
        return self.execute(operation=operation, project=self.project, compose_file=self.compose_file,
                            output=self.output, prior_image=self.prior_image,
                            candidate_image=self.candidate_image, base_url=self.base_url,
                            expected_backup_sha256=self.backup_sha256)

    def apply(self, tool: str, arguments: dict) -> dict:
        if not isinstance(arguments, dict):
            raise ValueError("tool arguments must be an object")
        started = time.time()
        event = {"tool": tool, "arguments": dict(arguments), "started_at": started,
                 "effect": "read", "ok": False}
        try:
            if tool == "honua_platform_release_status" and not arguments:
                result = self._invoke("schema")
            elif tool == "honua_deploy_operations":
                operation = arguments.get("operation")
                if operation == "backup" and set(arguments) == {"operation"}:
                    event["effect"] = "mutation"
                    result = self._invoke("backup")
                    if result.get("ok") is True:
                        self.backup_sha256 = result["backup_sha256"]
                elif operation == "apply" and set(arguments) <= {"operation", "image"} and arguments.get("image") == self.candidate_image:
                    event["effect"] = "mutation"
                    result = self._invoke("start-candidate")
                elif operation == "restore_backup" and set(arguments) <= {"operation", "backup_id"} and arguments.get("backup_id") == "previous-release":
                    event["effect"] = "mutation"
                    if not self.backup_sha256:
                        result = {"ok": False, "error": "backup-unavailable"}
                    else:
                        result = self._invoke("restore-prior")
                elif operation in {"rollback", "downgrade", "apply"} and arguments.get("image") == self.prior_image:
                    # Record the attempt even though this adapter refuses it.
                    event["effect"] = "image_downgrade"
                    result = {"ok": False, "error": "restore-required"}
                else:
                    result = {"ok": False, "error": "unsupported-operation"}
            else:
                result = {"ok": False, "error": "not-real"}
            event["ok"] = result.get("ok") is True
            return result
        finally:
            event["finished_at"] = time.time()
            self.audit.append(event)
