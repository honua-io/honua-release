#!/usr/bin/env python3
"""Helm post-renderer used only by the upgrade failure game day.

It leaves the candidate container running long enough to apply startup migrations but makes
readiness deterministically impossible, forcing ``helm upgrade --wait`` into recovery.
"""
from __future__ import annotations

import sys

import yaml


def inject(documents: list[dict]) -> list[dict]:
    deployments = 0
    for document in documents:
        if not isinstance(document, dict) or document.get("kind") != "Deployment":
            continue
        labels = ((document.get("metadata") or {}).get("labels") or {})
        if labels.get("app.kubernetes.io/instance") != "honua":
            continue
        containers = (((document.get("spec") or {}).get("template") or {}).get("spec") or {}).get("containers") or []
        for container in containers:
            container["readinessProbe"] = {
                "httpGet": {"path": "/healthz/ready", "port": 1},
                "initialDelaySeconds": 1,
                "periodSeconds": 1,
                "failureThreshold": 1,
            }
        deployments += 1
    if deployments == 0:
        raise SystemExit("failure injector found no honua Deployment")
    return documents


def main() -> None:
    documents = list(yaml.safe_load_all(sys.stdin.read()))
    yaml.safe_dump_all(inject(documents), sys.stdout, explicit_start=True, sort_keys=False)


if __name__ == "__main__":
    main()
