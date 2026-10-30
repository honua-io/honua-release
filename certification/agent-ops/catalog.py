"""Published MCP tool names. Loaded from the roster fixture, not invented here."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

ROSTER = (
    Path(__file__).resolve().parents[2]
    / "tools"
    / "fixtures"
    / "mcp-roster"
    / "mcp-tool-roster.v1.json"
)


@lru_cache(maxsize=1)
def load_roster() -> dict:
    return json.loads(ROSTER.read_text(encoding="utf-8"))


def advertised_tools() -> list[str]:
    """Operate view, durable admin tools, and the published deploy and read names."""
    roster = load_roster()
    names = set(roster["views"]["operate"])
    names.update(roster["requiresDurableControlPlane"])
    for name in roster["static"]:
        if name.startswith((
            "honua_propose_",
            "honua_deploy_",
            "honua_publish_",
            "honua_query_",
            "honua_list_",
            "honua_describe_",
            "honua_render_",
            "honua_studio_reopen",
        )):
            names.add(name)
    return sorted(names)


def roster_names() -> set[str]:
    roster = load_roster()
    names = set(roster["static"])
    names.update(roster["projectedAdmin"])
    return names
