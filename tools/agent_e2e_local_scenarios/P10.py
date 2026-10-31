"""P10 controller-side query replay helpers.

The full local P10 lifecycle is not yet wired; these helpers do not produce a
case receipt. Replay accepts only a digest-bound harness action, never prose.
"""
from __future__ import annotations

import base64
import copy
import hashlib

from agent_e2e_mcp import parse

from agent_e2e_local_runner import read_logged_action
from agent_e2e_p10_observer import query_ids


def replay_query(action_path, action_digest, binding, client):
    """Replay the recorded query once through a controller-authenticated client.

    The caller supplies a fresh initialized controller client and must retain
    its raw observations. The model-selected arguments are preserved verbatim.
    """
    action = read_logged_action(action_path, action_digest)
    if (set(action) != {'kind', 'tool', 'arguments'} or action['kind'] != 'tool_call'
            or action['tool'] != 'honua_query_features' or not isinstance(action['arguments'], dict)):
        raise ValueError('recorded action is not a P10 feature query')
    arguments = copy.deepcopy(action['arguments'])
    if (arguments.get('serviceId') != binding.get('service')
            or arguments.get('layerId') != binding.get('layerId')):
        raise ValueError('recorded query does not target the seeded layer')
    before = len(client.observations)
    result = client.call_tool(action['tool'], arguments)
    rows = client.observations[before:]
    calls = [row for row in rows if row.get('rpcMethod') == 'tools/call']
    if len(calls) != 1 or calls[0].get('httpStatus') != 200:
        raise ValueError('independent query response is missing or ambiguous')
    raw = base64.b64decode(calls[0]['bodyBase64'], validate=True)
    if calls[0].get('bodyDigest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('independent query response digest mismatch')
    if raw.lstrip().startswith(b'{'):
        messages = [parse(raw)]
    else:
        frames = raw.replace(b'\r\n', b'\n').split(b'\n\n')
        if frames[-1].strip():
            raise ValueError('incomplete independent query response')
        messages = [parse(data) for frame in frames[:-1]
                    if (data := b'\n'.join(line[5:].lstrip(b' ') for line in frame.split(b'\n')
                                           if line.startswith(b'data:')))]
    responses = [message for message in messages if isinstance(message, dict) and 'result' in message]
    if len(responses) != 1 or responses[0]['result'] != result:
        raise ValueError('independent query projection differs from raw response')
    observed = query_ids(responses[0]['result'], arguments, binding)
    return observed, {'replayedRequestDigest': action_digest, 'controllerResponses': copy.deepcopy(calls)}
