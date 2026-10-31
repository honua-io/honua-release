from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Stack:
    """A running local-docker candidate stack, as set up by the runner."""
    base_url: str                 # e.g. http://127.0.0.1:8147
    admin_key: str                # controller credential; never shown to the model
    approver_key: str | None      # separate approver principal (for P14/P17)
    compose_project: str
    compose_file: str
    workdir: str                  # per-case scratch dir (controller side)
    candidate_digest: str
    extra: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class Seeded:
    """What seed() created; passed back to observe()/cleanup(). Never shown to the model."""
    state: dict[str, Any]

@dataclass(frozen=True)
class Observation:
    assertion_id: str             # pack assertion id, e.g. "filter.exact"
    observed: Any                 # shape that corpus/oracle.verify expects for this assertion
    evidence: dict[str, Any]      # raw controller reads: {"requests":[{method,url,status,sha256,body_b64}], ...}

@dataclass(frozen=True)
class RunRecord:
    """Controller-recorded session facts; hints are never evidence by themselves."""
    agent_principal: str
    sessions: int
    fault_events: tuple[dict, ...]
    resource_hints: tuple[dict, ...]
    final_answers: tuple[str, ...]

class Scenario:
    id: str                       # "P10"
    supported_variants: tuple[str, ...]   # subset of ("A","B","C")
    def seed(self, stack: Stack, variant: str) -> Seeded: ...
    def fault_plan(self, variant: str) -> dict | None: ...   # e.g. P21: {"kind":"http_503_once","after":"map.created"}; else None
    def needs_approver(self) -> bool: ...
    def sessions_required(self) -> int: return 1
    def observe(self, stack: Stack, seeded: Seeded, variant: str, run: RunRecord | None = None) -> list[Observation]: ...
    def expected(self, variant: str) -> dict[str, Any]:   # assertion_id -> expected value for oracle.verify
        ...
    def cleanup(self, stack: Stack, seeded: Seeded) -> None: ...
