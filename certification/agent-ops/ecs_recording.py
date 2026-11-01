"""Capture proposal IDs inside the runner-owned recording proxy callback.

The callback runs in the proxy process, on actual MCP responses. Model prose and
CLI transcripts are never evidence. The resulting in-memory records must only be
consumed within a trusted boundary; this module does not authenticate artifacts.
"""
from __future__ import annotations

import json

from product_ops import proposal_ids


class ProposalCapture:
    def __init__(self, execute):
        self.execute = execute
        self.records = []

    def __call__(self, kind, action):
        result = self.execute(kind, action)
        if (kind != 'tool_call' or not isinstance(action, dict) or
                not str(action.get('tool', '')).startswith('honua_propose_') or
                not isinstance(result, dict) or result.get('status') not in ('ok', 'pass')):
            return result
        body = result.get('result')
        if not isinstance(body, dict) or body.get('isError') is True:
            return result
        payloads = [body.get('structuredContent')]
        for item in body.get('content', []):
            if isinstance(item, dict) and item.get('type') == 'text':
                try:
                    payloads.append(json.loads(item.get('text', '')))
                except (TypeError, ValueError):
                    pass
        ids = []
        for payload in payloads:
            for identifier in proposal_ids(payload):
                if identifier not in ids:
                    ids.append(identifier)
        for identifier in ids:
            self.records.append({'tool': action['tool'], 'proposalId': identifier})
        return result
