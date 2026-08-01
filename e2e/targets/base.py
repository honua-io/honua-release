"""Deploy-target contract.

A target is BLOCKED (not failed) when the infra to run it isn't wired — AGENTS.md honesty: the cloud
tier reports BLOCKED, never a fake green, until OIDC creds / a deployable image / the IaC are wired.
`require_real` (the release train / a nightly run) promotes BLOCKED to a hard failure so the gate can
genuinely fail once the infra exists.

Every target is exercised twice — `redis_enabled` True and False — so the platform is proven to behave
identically with and without its Redis cache (a cache must not change correctness).
"""
from __future__ import annotations

from dataclasses import dataclass, field


class ProvisionError(RuntimeError):
    """Provisioning the target failed (terraform apply error, no endpoint output, etc.)."""


@dataclass
class Availability:
    ok: bool
    reason: str
    missing: list[str] = field(default_factory=list)   # the specific prerequisites that are absent


class DeployTarget:
    """Subclasses implement availability/provision/teardown for one deploy target."""

    name: str = "base"
    supports_redis: bool = True       # whether the target can toggle Redis on/off

    def availability(self) -> Availability:  # pragma: no cover - interface
        raise NotImplementedError

    def provision(self, redis_enabled: bool = False) -> str:
        """Provision (with Redis on/off) and return honua-server's public base URL. Raises ProvisionError."""
        raise NotImplementedError  # pragma: no cover - interface

    def teardown(self, redis_enabled: bool | None = None) -> None:
        """Best-effort teardown. MUST be safe to call even if provision() partially ran."""
        raise NotImplementedError  # pragma: no cover - interface
