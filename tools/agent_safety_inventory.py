"""The candidate's governed change-route inventory (honua-server agent-change-routes.txt).

The server commits every governed route that changes state, one "<METHOD> <template>" per line,
kept current by AgentChangeRouteInventoryTests. The walk calls every one of them as the agent, so
coverage does not depend on what the admin OpenAPI document happens to describe. The list is the
test host's route table with experimental capabilities on, and the agent-safety stack turns them
on too. An inventory route is judged like an OpenAPI one: an empty 404, 405 or 415 counts as not
mapped only when the route fixtures give the reason, because a handler that admitted the agent can
answer the same way. ANY means the endpoint takes every method and is called with POST.
"""
from __future__ import annotations

import re

from agent_safety_routes import normalize

INVENTORY = 'tests/dotnet/Honua.Server.Tests/Features/Security/agent-change-routes.txt'
PARAMETER = re.compile(r'\{\**([^}:?]+)(?::([^}?]+))?\??\}')
TYPED = {'int': {'type': 'integer'}, 'long': {'type': 'integer'}, 'guid': {'type': 'string', 'format': 'uuid'}}


def parse_inventory(text: str) -> list[dict]:
    """'<METHOD> <template>' lines -> route records with path parameters typed by their constraints."""
    routes = []
    for line in text.splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        method, template = line.split(' ', 1)
        template = template.strip() if template.strip().startswith('/') else '/' + template.strip()
        parameters = {name: {'schema': TYPED.get(constraint or '', {'type': 'string'})}
                      for name, constraint in PARAMETER.findall(template) if constraint != 'apiVersion'}
        routes.append({'method': 'POST' if method == 'ANY' else method.upper(),
                       'template': re.sub(r'\{\*+', '{', normalize(template)), 'parameters': parameters,
                       'contentType': 'application/json', 'anonymous': False})
    return routes


def inventory_routes(inventory: list[dict], known: set) -> list[dict]:
    """Inventory routes the OpenAPI document and the rule list did not already contribute."""
    added, seen = [], set(known)
    for route in inventory:
        key = (route['method'], route['template'])
        if key not in seen:
            seen.add(key)
            added.append(route)
    return added


def load_inventory(path) -> list[dict] | None:
    """The committed inventory, or None when the candidate revision has none (the walk then fails closed)."""
    try:
        routes = parse_inventory(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None
    return routes or None


def uncovered(inventory: list[dict] | None, walked: list[dict]) -> list[dict]:
    """Unchecked results for inventory routes the walk never called; a missing inventory is itself a gap."""
    if inventory is None:
        return [{'method': 'FILE', 'route': INVENTORY, 'path': None, 'status': 'missing at the candidate revision',
                 'verdict': 'unchecked'}]
    called = {(r['method'], r['route']) for r in walked}
    return [{'method': r['method'], 'route': r['template'], 'path': None, 'status': 'not called',
             'verdict': 'unchecked'} for r in inventory if (r['method'], r['template']) not in called]
