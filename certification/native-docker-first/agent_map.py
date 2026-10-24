"""Same external conversation Studio continuation. Offline joins are not live evidence."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from urllib.parse import quote

MAP_PROMPT = ('Add this service to the map I am editing, style it blue, save a version, '
              'and reopen that version for continued editing. Do not publish it.')
MAP_SUMMARY = 'The map draft is ready for your review. Publication still needs your action.'
MAP_TOOLS = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_studio_get_draft', 'honua_studio_add_layer', 'honua_studio_set_layer_style',
             'honua_studio_save_version', 'honua_studio_get_version', 'honua_studio_reopen_version',
             'honua_workspace_set_context', 'honua_workspace_get', 'honua_workspace_append_message')


def body_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def structured_results(runner, transcript, calls):
    outputs = {}
    for line in transcript.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict):
            continue
        raw, message = event.get('tool_use_result'), event.get('message')
        results = [item for item in message.get('content', []) if isinstance(item, dict)
                   and item.get('type') == 'tool_result'] if isinstance(message, dict) else []
        if isinstance(raw, dict) and isinstance(raw.get('structuredContent'), dict):
            if len(results) != 1 or raw.get('isError'):
                raise runner.Blocked('ambiguous or failed original MCP Studio result')
            key = results[0].get('tool_use_id')
            if key in outputs:
                raise runner.Blocked('duplicate original MCP Studio result')
            outputs[key] = raw['structuredContent']
    if any(call['id'] not in outputs for call in calls):
        raise runner.Blocked('Studio continuation lacks original structured MCP results')
    return outputs


def observed_map(runner, transcript, service, original, turn_id):
    calls, results, positions, result_positions = runner.ordered_tool_events(transcript)
    permitted = {'mcp__honua__' + name for name in MAP_TOOLS} | {
        'ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers',
        'mcp__honua__honua_agent_status'}
    if any(call.get('name') not in permitted for call in calls):
        raise runner.Blocked('Studio continuation contains unrelated mutation or publication tool')
    sequence = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
                'honua_studio_get_draft', 'honua_studio_add_layer', 'honua_studio_set_layer_style',
                'honua_studio_save_version', 'honua_studio_get_version', 'honua_studio_reopen_version',
                'honua_studio_get_draft', 'honua_workspace_set_context', 'honua_workspace_get', 'honua_workspace_append_message')
    relevant = [call for call in calls if call.get('name') in {'mcp__honua__' + name for name in MAP_TOOLS}]
    if [call['name'] for call in relevant] != ['mcp__honua__' + name for name in sequence]:
        raise runner.Blocked('Studio continuation must edit, save, reopen and bind the same map exactly once')
    runner.require_results_before_dependencies(relevant, results, positions, result_positions)
    outputs = structured_results(runner, transcript, relevant)
    conversation, turn, bound, before, added, styled, saved, version_read, reopened, after, linked, current, summary = (
        outputs[call['id']] for call in relevant)
    locator = {'workspaceId': original['workspaceId'], 'turnId': turn_id}
    context = turn.get('context') or {}
    task = context.get('task') or {}
    for result in (conversation, turn):
        messages = [m for m in result.get('messages', []) if m.get('role') == 'user' and m.get('turnId') == turn_id]
        if len(messages) != 1 or messages[0].get('content') != MAP_PROMPT:
            raise runner.Blocked('Studio continuation lacks the actual human map request')
    if (relevant[0]['input'] != {'workspaceId': original['workspaceId']} or
            relevant[1]['input'] != locator or relevant[2]['input'] != locator or
            turn.get('turnId') != turn_id or turn_id in (original['turnId'], service['turnId']) or
            any(context.get(key) != original.get(key) for key in ('workspaceId', 'targetId', 'tenantId')) or
            task.get('kind') != 'studio-composition' or task.get('id') != before.get('draftId') or
            task.get('revision') != before.get('generation') or task.get('packageKey') != before.get('packageKey') or
            bound.get('state') != 'attached' or bound.get('turnBinding') != locator):
        raise runner.Blocked('Studio continuation does not bind the human canonical map draft')
    for value in (before, added, styled, reopened, after):
        if (not isinstance(value.get('draftId'), str) or not value['draftId'] or
                type(value.get('generation')) is not int or value['generation'] < 1 or
                value.get('workspaceId') != original['workspaceId'] or value.get('family') != 'map'):
            raise runner.Blocked('Studio continuation lacks a canonical map identity/generation')
    identity = {key: before.get(key) for key in ('draftId', 'itemId', 'packageKey', 'workspaceId')}
    if (any(not value for value in identity.values()) or
            relevant[3]['input'] != {'draftId': identity['draftId']} or
            before.get('envelope', {}).get('body', {}).get('layers') not in (None, []) or
            any(any(value.get(key) != expected for key, expected in identity.items()) for value in (added, styled))):
        raise runner.Blocked('Studio continuation changed map identity or did not start from the human empty draft')
    add_input, style_input = relevant[4]['input'], relevant[5]['input']
    layer = add_input.get('layer') or {}
    layer_id = layer.get('id')
    if (set(add_input) != {'draftId', 'generation', 'layer'} or
            add_input.get('draftId') != identity['draftId'] or add_input.get('generation') != before['generation'] or
            not isinstance(layer_id, str) or not layer_id or layer.get('sourceId') != service['studioSourceId'] or
            added['generation'] <= before['generation'] or styled['generation'] <= added['generation'] or
            style_input.get('draftId') != identity['draftId'] or style_input.get('generation') != added['generation'] or
            style_input.get('layerId') != layer_id or set(style_input) != {'draftId', 'generation', 'layerId', 'styleBody'}):
        raise runner.Blocked('Studio edit does not use the observed source and latest generation')
    style_body = style_input.get('styleBody') or {}
    paint = style_body.get(layer_id, {}).get('paint') or {}
    if set(style_body) != {layer_id} or not any(paint.get(key) == '#2563eb' for key in
                                               ('fill-color', 'line-color', 'circle-color', 'text-color')):
        raise runner.Blocked('Studio refinement does not declare the requested map-local blue style')
    saved_version = saved.get('version') or {}
    for value in (saved.get('operation') or {}, reopened.get('operation') or {}):
        if value.get('status') != 'Completed' or value.get('requiresApproval') is not False or not value.get('operationInstanceId'):
            raise runner.Blocked('Studio save/reopen is denied, pending or lacks durable operation identity')
    version_identity = {key: saved_version.get(key) for key in ('itemId', 'versionId', 'contentHash')}
    if (version_identity['itemId'] != identity['itemId'] or not version_identity['versionId'] or
            not runner.DIGEST.fullmatch(version_identity.get('contentHash') or '') or
            relevant[6]['input'].get('draftId') != identity['draftId'] or relevant[6]['input'].get('generation') != styled['generation'] or
            relevant[7]['input'] != {key: version_identity[key] for key in ('itemId', 'versionId')} or
            any(version_read.get(key) != version_identity[key] for key in ('versionId', 'contentHash')) or
            relevant[8]['input'] != relevant[7]['input'] or relevant[9]['input'] != {'draftId': reopened['draftId']} or
            reopened['draftId'] == identity['draftId'] or reopened.get('baseVersionId') != version_identity['versionId'] or
            any(reopened.get(key) != identity[key] or after.get(key) != identity[key] for key in ('itemId', 'packageKey', 'workspaceId')) or
            after.get('draftId') != reopened['draftId'] or after.get('generation') != reopened['generation']):
        raise runner.Blocked('saved version/readback/reopened draft identities differ')
    before_body = before.get('envelope', {}).get('body') or {}
    body = styled.get('envelope', {}).get('body')
    # Preserve unrelated author work, including controls/widgets and viewport.
    # The add-layer adapter may extend render sources/layers, but may not rewrite
    # existing map settings or sources while adding this one presentation layer.
    unrelated = lambda value: {key: content for key, content in value.items() if key not in ('layers', 'styleRefs', 'mapSpec')}
    if not isinstance(body, dict) or unrelated(before_body) != unrelated(body):
        raise runner.Blocked('map continuation rewrote unrelated human draft content')
    old_spec, new_spec = before_body.get('mapSpec') or {}, body.get('mapSpec') or {}
    if (any(new_spec.get(key) != value for key, value in old_spec.items() if key not in ('layers', 'sources')) or
            any((new_spec.get('sources') or {}).get(key) != value for key, value in (old_spec.get('sources') or {}).items()) or
            any(layer not in (new_spec.get('layers') or []) for layer in old_spec.get('layers') or []) or
            any(reference not in (body.get('styleRefs') or []) for reference in before_body.get('styleRefs') or [])):
        raise runner.Blocked('map continuation rewrote existing render settings, sources or styles')
    if (not isinstance(body, dict) or any(value.get('envelope', {}).get('body') != body for value in
                                        (saved_version, reopened, after)) or
            len(body.get('layers') or []) != 1 or body['layers'][0].get('id') != layer_id or
            body['layers'][0].get('sourceId') != service['studioSourceId'] or
            not any(ref.get('body') == style_body for ref in body.get('styleRefs', []))):
        raise runner.Blocked('independent MCP draft/version reads do not retain the exact source and refinement')
    expected_task = {'kind': 'studio-composition', 'id': reopened['draftId'], 'revision': reopened['generation'],
                     'packageKey': identity['packageKey']}
    expected_link = {'workspaceId': original['workspaceId'], 'viewId': context.get('viewId'),
                     'expectedRevision': context.get('revision'), 'targetId': original['targetId'],
                     'selectedResources': [], 'task': expected_task}
    if (not expected_link['viewId'] or type(expected_link['expectedRevision']) is not int or
            relevant[10]['input'] != expected_link or relevant[11]['input'] != {
                'workspaceId': original['workspaceId'], 'viewId': context['viewId']} or
            any(value.get('task') != expected_task or value.get('workspaceId') != original['workspaceId'] or
                value.get('targetId') != original['targetId'] for value in (linked, current))):
        raise runner.Blocked('reopened canonical draft is not the same Console workspace task')
    summary_input = relevant[12]['input']
    if (set(summary_input) != {'workspaceId', 'turnId', 'messageId', 'content'} or
            summary_input.get('workspaceId') != original['workspaceId'] or summary_input.get('turnId') != turn_id or
            summary_input.get('content') != MAP_SUMMARY or not summary_input.get('messageId') or
            summary.get('id') != summary_input['messageId'] or summary.get('turnId') != turn_id or
            summary.get('role') != 'assistant' or summary.get('content') != MAP_SUMMARY or
            summary.get('evidenceKind') != 'conversation'):
        raise runner.Blocked('external map continuation did not persist its bounded handoff in the same Console conversation')
    return {**identity, **version_identity, 'turnId': turn_id, 'viewId': context['viewId'],
            'targetId': original['targetId'], 'tenantId': original['tenantId'], 'layerId': layer_id,
            'studioSourceId': service['studioSourceId'], 'reopenedDraftId': reopened['draftId'],
            'reopenedGeneration': reopened['generation'], 'bodyCanonicalSha256': body_hash(body),
            'operationInstanceIds': [saved['operation']['operationInstanceId'], reopened['operation']['operationInstanceId']],
            'assistantMessageId': summary['id'], 'sameExternalSession': True, 'publication': 'awaiting-real-human-proposer', 'renderedMapVerified': False}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def server_reader(runner, state):
    origin = state['target']['origins']['server']
    if not re.fullmatch(r'http://127\.0\.0\.1:\d+', origin):
        raise runner.Blocked('independent Studio read requires the exact installed loopback server')
    values = dict(line.split('=', 1) for line in (Path(state['installDirectory']) / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    credential = values['HONUA_AGENT_API_KEY'].strip('"\'')
    opener = build_opener(ProxyHandler({}), NoRedirect())
    def read(path):
        try:
            with opener.open(Request(origin + path, headers={'X-API-Key': credential, 'Accept': 'application/json'}), timeout=30) as response:
                data = response.read(2_097_153)
                if response.status != 200 or len(data) > 2_097_152:
                    raise runner.Blocked('independent Studio read failed or exceeded its bound')
                value = json.loads(data)
                return value.get('data', value)
        except Exception:
            raise runner.Blocked('independent Studio read unavailable; original map must not be replayed') from None
    return read


def independent_map_readback(runner, state, map_receipt, *, read=None):
    """Controller-only GETs; never inject results into the external model transcript."""
    read = read or server_reader(runner, state)
    item, version = (quote(map_receipt[key], safe='') for key in ('itemId', 'versionId'))
    saved = read(f'/api/v1/studio/content-items/{item}/versions/{version}')
    drafts = [read('/api/v1/studio/package-drafts/' + quote(map_receipt[key], safe=''))
              for key in ('draftId', 'reopenedDraftId')]
    if (any(saved.get(key) != map_receipt[key] for key in ('itemId', 'versionId', 'contentHash')) or
            any(draft.get('draftId') != map_receipt[key] or draft.get('itemId') != map_receipt['itemId'] or
                draft.get('workspaceId') != map_receipt['workspaceId'] for draft, key in zip(drafts, ('draftId', 'reopenedDraftId'))) or
            any(body_hash(value.get('envelope', {}).get('body')) != map_receipt['bodyCanonicalSha256'] for value in [saved, *drafts])):
        raise runner.Blocked('independent canonical HTTP map/version observations differ from external MCP results')
    return {'status': 'pass', 'bodyCanonicalSha256': map_receipt['bodyCanonicalSha256'],
            'itemId': map_receipt['itemId'], 'versionId': map_receipt['versionId'],
            'contentHash': map_receipt['contentHash'], 'renderedMapVerified': False}


def validate_session(runner, state, binding, attempt):
    if runner.installed_target(Path(state['installDirectory']), binding) != state.get('target'):
        raise runner.Blocked('installed target changed before external map continuation')
    config, executable = attempt / 'mcp.json', Path(state['executables']['mcp'])
    if (not config.is_file() or not executable.is_file() or runner.sha256(config) != state.get('mcpConfigSha256') or
            runner.sha256(executable) != state.get('mcpExecutableSha256')):
        raise runner.Blocked('external registration changed before map continuation')
    return config


def phase_map(runner, args, state, binding, attempt):
    if state['stages'].get('agentService') != 'pass' or state['stages'].get('agentMap'):
        raise runner.Blocked('map continuation needs a verified external service and cannot replay an uncertain effect')
    original, service = state['agentImportTask'], state['agentService']
    if (args.workspace_id != original['workspaceId'] or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.turn_id or '') or
            args.turn_id in (original['turnId'], service['turnId'])):
        raise runner.Blocked('a fresh actual human map turn in the same workspace is required')
    config = validate_session(runner, state, binding, attempt)
    state['stages']['agentMap'] = 'started'
    state['agentMapIntent'] = {'workspaceId': args.workspace_id, 'turnId': args.turn_id}
    runner.private_json(attempt / 'receipt.json', state)
    prompt = (f'Continue the SAME external conversation and registration. The human opened an empty map in the '
              f'existing Console workspace {args.workspace_id} and sent actual turn {args.turn_id}: "{MAP_PROMPT}". '
              f'Read honua_workspace_conversation then honua_workspace_get_turn, verify that actual prompt and '
              f'studio-composition task, bind that workspace/turn with honua_agent_connect. Read that exact task '
              f'with honua_studio_get_draft. Never create a substitute map. Add exactly one presentation layer '
              f'with honua_studio_add_layer using layer.sourceId {service["studioSourceId"]}; retain its returned '
              f'draftId and latest generation. Refine it with honua_studio_set_layer_style using only styleBody '
              f'{{"<returned presentation layer id>":{{"paint":{{"<correct geometry color property>":"#2563eb"}}}}}}. '
              f'Use the actual map/source geometry to choose fill-color, line-color or circle-color. Save once with '
              f'honua_studio_save_version, read its exact itemId/versionId with honua_studio_get_version, reopen '
              f'it once with honua_studio_reopen_version then read the reopened draft with honua_studio_get_draft. '
              f'Finally honua_workspace_set_context using the ORIGINAL captured viewId/revision/targetId, '
              f'selectedResources [], and task {{kind:"studio-composition",id:<reopened draftId>,revision:<generation>,'
              f'packageKey:<original packageKey>}}; then honua_workspace_get that view. Persist one bounded assistant message '
              f'with honua_workspace_append_message in the same workspace/turn, a new messageId, and exact content '
              f'"{MAP_SUMMARY}". This text is conversation, not success evidence. Inspect each result before '
              f'its dependent call. Do not call publication, approval, other tools or repeat a mutation; stop on '
              f'conflict, denied/pending approval, missing tools or uncertain response. The human will publish separately.')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + [
        'mcp__honua__' + name for name in (*MAP_TOOLS, 'honua_agent_status')]
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config',
               '--mcp-config', str(config), '--permission-mode', 'dontAsk', '--permission-prompts', 'none',
               '--resume', state['externalClientSessionId'], '--output-format', 'stream-json', '--verbose', '--print']
    try:
        result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=900, cwd=attempt, check=False)
        for name, content in (('agent-map-transcript.ndjson', result.stdout), ('agent-map-stderr.log', result.stderr)):
            path = attempt / name
            path.write_text(content)
            path.chmod(0o600)
        state['agentMapTranscriptSha256'] = runner.sha256(attempt / 'agent-map-transcript.ndjson')
        if result.returncode:
            raise runner.Blocked('external map continuation failed; reconcile existing drafts without replay')
        runner.observed_client_session(result.stdout, state['externalClientSessionId'])
        observed = observed_map(runner, result.stdout, service, original, args.turn_id)
        # Retain exact identities even if independent readback is denied or lost.
        state['agentMap'] = observed
        runner.private_json(attempt / 'receipt.json', state)
        state['agentMapReadback'] = independent_map_readback(runner, state, observed)
        state['stages']['agentMap'] = 'pass'
        state['stages']['agentPublication'] = 'awaiting-real-human-proposer'
        state['oneExternalAgentConversationQualified'] = False
        runner.private_json(attempt / 'external-map.json', observed, new=True)
        runner.private_json(attempt / 'receipt.json', state)
        print('Same external session edited/saved/reopened the canonical Console draft; real human publication and rendered/audience reads remain.')
    except Exception as exc:
        state['stages']['agentMap'] = 'blocked'
        state['blocker'] = str(exc)
        runner.private_json(attempt / 'receipt.json', state)
        raise


def observed_pending_publication(runner, state, handoff, observed, proposal_id, route, *, read=None):
    """Validate a live human/browser capture; never accept a supplied passing receipt."""
    if (any(handoff.get(key) != observed.get(key) for key in
            ('workspaceId', 'draftId', 'reopenedDraftId', 'itemId', 'versionId', 'contentHash', 'studioSourceId', 'bodyCanonicalSha256')) or
            handoff.get('status') != 'awaiting-separate-human-approval' or handoff.get('route') != route or
            handoff.get('proposalId') != proposal_id or handoff.get('renderedMapVerified') is not True or
            any(not isinstance(handoff.get(key), str) or not handoff[key].strip() for key in
                ('proposalId', 'requestedBy', 'operationInstanceId', 'commandId'))) or (
            'requestId' in handoff and
            (not isinstance(handoff['requestId'], str) or not handoff['requestId'].strip())):
        raise runner.Blocked('human pending publication capture differs from the external map or exact proposal')
    proposal = handoff.get('canonicalProposal') or {}
    expected_diff = [f'itemId={observed["itemId"]}', f'versionId={observed["versionId"]}',
                     f'contentHash={observed["contentHash"]}', f'route={route}', 'visibility=public']
    if (proposal.get('proposalId') != proposal_id or proposal.get('status', '').lower() != 'awaitingapproval' or
            proposal.get('requestedBy') != handoff['requestedBy'] or proposal.get('requestedByAgent') or
            proposal.get('resolvedBy') or proposal.get('diff') != expected_diff):
        raise runner.Blocked('canonical proposal is not an unresolved human request for the exact saved map and audience')
    read = read or server_reader(runner, state)
    operation = read('/api/v1/operations/handles/' + quote(handoff['operationInstanceId'], safe=''))
    if (operation.get('operationInstanceId') != handoff['operationInstanceId'] or
            operation.get('operationId') != 'studio.content.create-publication-request' or
            operation.get('proposalId') != proposal_id or operation.get('status', '').lower() != 'requiresapproval'):
        raise runner.Blocked('canonical publication operation is not pending on the captured human proposal')
    resources = operation.get('resourceIds') or {}
    if 'requestId' in resources and (not isinstance(resources['requestId'], str) or not resources['requestId'].strip()):
        raise runner.Blocked('pending canonical publication request identity is malformed')
    if resources and (any(resources.get(key) != handoff[key] for key in ('itemId', 'versionId')) or
                      (handoff.get('requestId') is not None and resources.get('requestId') != handoff['requestId'])):
        raise runner.Blocked('pending canonical publication resource identities differ')
    # Approval actuation creates the publication row. Pending receipts may have
    # no request ID; any ID already observed must match the final canonical one.
    if resources.get('requestId'):
        handoff = {**handoff, 'requestId': resources['requestId']}
    return {**handoff, 'requestIdentityVerified': bool(resources.get('requestId')),
            'pendingBinding': 'canonical-proposal-diff-and-operation',
            'commandIdentityObservedBy': 'human-console-journal'}


def valid_human_browser_cdp(value):
    if not isinstance(value, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}', value):
        return False
    return 0 < int(value.rsplit(':', 1)[1]) < 65536


def phase_publication(runner, args, state, binding, attempt):
    if state['stages'].get('agentMap') != 'pass' or state['stages'].get('agentPublication') not in (
            'awaiting-real-human-proposer', 'awaiting-human-proposal-capture', 'blocked-read-only-capture'):
        raise runner.Blocked('pending human publication capture needs the retained external map')
    validate_session(runner, state, binding, attempt)
    if (not valid_human_browser_cdp(args.human_browser_cdp) or
            args.origin != state['target']['origins']['console'] or
            not re.fullmatch(r'/[a-z0-9][a-z0-9/_-]{0,126}', args.route or '') or
            not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', args.proposal_id or '')):
        raise runner.Blocked('existing human loopback CDP browser, exact already-pending proposal ID, installed origin and route required')
    independent_map_readback(runner, state, state['agentMap'])
    if runner.read_json(attempt / 'external-map.json') != state['agentMap']:
        raise runner.Blocked('external map reference changed before read-only human proposal capture')
    helper = Path(__file__).with_name('external_map_publication.mjs')
    # Capture is entirely read-only and can be retried after missing/stale browser
    # state. It never submits, refresh-retries, approves or opens publication review.
    name = 'external-publication-capture-' + runner.uuid.uuid4().hex
    capture_path = attempt / (name + '.json')
    state['stages']['agentPublication'] = 'awaiting-human-proposal-capture'
    state['agentPublicationHelperSha256'] = runner.sha256(helper)
    runner.private_json(attempt / 'receipt.json', state)
    try:
        result = subprocess.run(['node', str(helper), str(args.console_root), args.origin, args.human_browser_cdp,
                                 str(attempt / 'external-map.json'), str(capture_path), args.route, args.proposal_id],
                                cwd=attempt, capture_output=True, text=True, timeout=300, check=False)
        log = attempt / (name + '.log')
        log.write_text(result.stdout + '\n' + result.stderr)
        log.chmod(0o600)
        if result.returncode:
            raise runner.Blocked('read-only pending human proposal/map capture unavailable; no publication was submitted')
        handoff = observed_pending_publication(runner, state, runner.read_json(capture_path), state['agentMap'], args.proposal_id, args.route)
        runner.private_json(attempt / 'external-publication-handoff.json', handoff, new=True)
        state['externalPublication'] = handoff
        state['stages']['agentPublication'] = 'awaiting-separate-human-approval'
        state.pop('blocker', None)
        runner.private_json(attempt / 'receipt.json', state)
        print('Already pending real human proposal captured read-only for the same external map; a different human must approve.')
    except Exception as exc:
        state['stages']['agentPublication'] = 'blocked-read-only-capture'
        state['blocker'] = str(exc)
        runner.private_json(attempt / 'receipt.json', state)
        raise


def observed_audience_resume(runner, transcript, original, observed):
    calls, results, positions, result_positions = runner.ordered_tool_events(transcript)
    sequence = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
                'honua_studio_get_draft', 'honua_studio_get_version')
    permitted = {'mcp__honua__' + name for name in sequence} | {
        'ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers',
        'mcp__honua__honua_agent_status'}
    if any(call.get('name') not in permitted for call in calls):
        raise runner.Blocked('audience continuation must remain read-only; no proposal or approval tool allowed')
    ordered = [call for call in calls if call['name'] in {'mcp__honua__' + name for name in sequence}]
    if [call['name'] for call in ordered] != ['mcp__honua__' + name for name in sequence]:
        raise runner.Blocked('external audience continuation must reread the original turn and exact saved map')
    runner.require_results_before_dependencies(ordered, results, positions, result_positions)
    outputs = structured_results(runner, transcript, ordered)
    conversation, turn, bound, draft, version = (outputs[call['id']] for call in ordered)
    locator = {'workspaceId': original['workspaceId'], 'turnId': observed['turnId']}
    if (ordered[0]['input'] != {'workspaceId': original['workspaceId']} or
            ordered[1]['input'] != locator or ordered[2]['input'] != locator or
            turn.get('turnId') != observed['turnId'] or
            bound.get('state') != 'attached' or bound.get('turnBinding') != locator or
            any(turn.get('context', {}).get(key) != original.get(key) for key in ('workspaceId', 'targetId', 'tenantId')) or
            not any(message.get('role') == 'user' and message.get('turnId') == observed['turnId'] and
                    message.get('content') == MAP_PROMPT for message in conversation.get('messages', [])) or
            ordered[3]['input'] != {'draftId': observed['reopenedDraftId']} or
            ordered[4]['input'] != {key: observed[key] for key in ('itemId', 'versionId')} or
            draft.get('draftId') != observed['reopenedDraftId'] or draft.get('itemId') != observed['itemId'] or
            draft.get('workspaceId') != original['workspaceId'] or
            body_hash(draft.get('envelope', {}).get('body')) != observed['bodyCanonicalSha256'] or
            any(version.get(key) != observed[key] for key in ('versionId', 'contentHash'))):
        raise runner.Blocked('external audience continuation lost its original workspace/map/version context')
    return {'status': 'pass', 'workspaceId': original['workspaceId'],
            'turnId': observed['turnId'], 'itemId': observed['itemId'], 'versionId': observed['versionId'],
            'contentHash': observed['contentHash'], 'sameExternalSession': True,
            'anonymousAudienceObservedBy': 'independent-controller-browser'}


def validate_audience_witness(runner, handoff, witness):
    if (witness.get('status') != 'pass' or
            any(witness.get(key) != handoff.get(key) for key in
                ('proposalId', 'operationInstanceId', 'commandId', 'itemId', 'versionId', 'contentHash', 'route', 'requestedBy')) or
            not isinstance(witness.get('requestId'), str) or not witness['requestId'].strip() or
            (handoff.get('requestId') is not None and witness['requestId'] != handoff['requestId']) or
            not isinstance(witness.get('requestedBy'), str) or not witness['requestedBy'].strip() or
            not isinstance(witness.get('resolvedBy'), str) or not witness['resolvedBy'].strip() or
            witness['requestedBy'] == witness['resolvedBy']):
        raise runner.Blocked('audience witness lacks exact map/publication and distinct real human identities')


def _phase_audience(runner, args, state, binding, attempt):
    if (state['stages'].get('agentMap') != 'pass' or
            state['stages'].get('agentPublication') not in ('awaiting-separate-human-approval', 'pass')):
        raise runner.Blocked('same-map audience continuation needs the original pending human publication')
    config = validate_session(runner, state, binding, attempt)
    if (not args.operator_state or not args.operator_state.is_file() or
            args.origin != state['target']['origins']['console'] or args.server_origin != state['target']['origins']['server']):
        raise runner.Blocked('original paired human state and installed origins required for audience witness')
    observed, handoff = state['agentMap'], state['externalPublication']
    if runner.read_json(attempt / 'external-publication-handoff.json') != handoff:
        raise runner.Blocked('external publication handoff changed after human proposal')
    # Always obtain a fresh canonical approval/publication/anonymous observation,
    # including after a completed phase. Never accept a supplied witness file.
    witness_path = attempt / ('external-audience-witness-' + runner.uuid.uuid4().hex + '.json')
    values = dict(line.split('=', 1) for line in (Path(state['installDirectory']) / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    env = {**runner.os.environ, 'HONUA_WORKSPACE_E2E_ORIGIN': args.origin,
           'HONUA_WORKSPACE_E2E_OPERATOR_STATE': str(args.operator_state),
           'HONUA_WORKSPACE_E2E_SERVER_ORIGIN': args.server_origin,
           'HONUA_WORKSPACE_E2E_AGENT_KEY': values['HONUA_AGENT_API_KEY'].strip('"\''),
           'HONUA_WORKSPACE_E2E_PUBLICATION_HANDOFF': str(attempt / 'external-publication-handoff.json'),
           'HONUA_WORKSPACE_E2E_PUBLICATION_WITNESS': str(witness_path)}
    result = subprocess.run(runner.playwright_command(args.console_root, 'publication-witness.live.spec.ts'),
                            cwd=args.console_root / 'e2e/playwright', env=env,
                            capture_output=True, text=True, timeout=300, check=False)
    log = attempt / ('external-witness-' + runner.uuid.uuid4().hex + '.log')
    log.write_text(result.stdout + '\n' + result.stderr)
    log.chmod(0o600)
    if result.returncode:
        state['stages']['agentAudience'] = 'awaiting-canonical-approval-and-audience'
        runner.private_json(attempt / 'receipt.json', state)
        raise runner.Blocked('separate human approval or anonymous audience read is pending/failed; no mutation was repeated')
    witness = runner.read_json(witness_path)
    validate_audience_witness(runner, handoff, witness)
    state['externalAudienceWitness'] = {'path': witness_path.name, 'sha256': runner.sha256(witness_path)}
    runner.private_json(attempt / 'receipt.json', state)
    # Read-only resume may be repeated. Each transcript gets a new immutable name;
    # no mutation or publication tool is granted to this client phase.
    names = ('honua_workspace_conversation', 'honua_workspace_get_turn', 'honua_agent_connect',
             'honua_studio_get_draft', 'honua_studio_get_version', 'honua_agent_status')
    allowed = ['ToolSearch', 'ListMcpResourcesTool', 'ReadMcpResourceTool', 'WaitForMcpServers'] + ['mcp__honua__' + name for name in names]
    prompt = (f'Continue the SAME external conversation and registration, READ ONLY. Re-read honua_workspace_conversation '
              f'workspaceId {observed["workspaceId"]}, then honua_workspace_get_turn turnId {observed["turnId"]} '
              f'in that workspace; bind that original turn with honua_agent_connect. Read the original reopened '
              f'draftId {observed["reopenedDraftId"]} with honua_studio_get_draft and saved itemId '
              f'{observed["itemId"]}/versionId {observed["versionId"]} with honua_studio_get_version. '
              f'Observe their actual identities/hash. No writes, new turns, approvals, publication, claims of audience '
              f'access from model text, or replacement drafts. The independent human/controller witness checks audience separately.')
    command = [args.claude, '--restricted', '--tools', 'default', '--allowedTools', ','.join(allowed),
               '--disallowedTools', 'Bash,PowerShell,Agent,Read,Write,Edit,NotebookEdit,Glob,Grep,LSP,WebFetch,WebSearch,Workflow,Task',
               '--setting-sources', '', '--settings', '{"disableAllHooks":true}', '--strict-mcp-config', '--mcp-config', str(config),
               '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--resume', state['externalClientSessionId'],
               '--output-format', 'stream-json', '--verbose', '--print']
    result = subprocess.run(command, input=prompt, text=True, capture_output=True, timeout=900, cwd=attempt, check=False)
    name = 'agent-audience-' + runner.uuid.uuid4().hex
    transcript = attempt / (name + '.ndjson')
    for path, text in ((transcript, result.stdout), (attempt / (name + '.log'), result.stderr)):
        path.write_text(text)
        path.chmod(0o600)
    if result.returncode:
        raise runner.Blocked('read-only same-session audience continuation failed; no mutation replayed')
    runner.observed_client_session(result.stdout, state['externalClientSessionId'])
    resumed = observed_audience_resume(runner, result.stdout, state['agentImportTask'], observed)
    independent_map_readback(runner, state, observed)
    if any(state['stages'].get(stage) != 'pass' for stage in
           ('prepare', 'install', 'agentTask', 'agentSubmission', 'agentImportObservation', 'agentService', 'agentMap')):
        raise runner.Blocked('whole external chain lacks an earlier verified stage')
    state['agentAudienceResume'] = {**resumed, 'transcriptSha256': runner.sha256(transcript)}
    state['externalAudience'] = witness
    state['stages']['agentAudience'] = 'pass'
    state['stages']['agentPublication'] = 'pass'
    state['status'] = 'same-external-agent-with-human-governance-pass'
    state['oneExternalAgentConversationQualified'] = True
    state.pop('blocker', None)
    state['qualificationScope'] = 'guided local Docker external import/service/map/save/reopen with actual human proposal, separate approval and independently observed audience; not AI-01..05 pilot qualification'
    runner.private_json(attempt / 'receipt.json', state)
    print('Same external conversation and canonical Console map matched real human governance and independent audience reads. Broader pilot profile remains unqualified.')


def _phase_observe_map(runner, args, state, binding, attempt):
    """Recover an independent GET failure after a fully joined MCP map receipt."""
    if not isinstance(state.get('agentMap'), dict):
        raise runner.Blocked('no fully observed external map receipt; uncertain mutations require operator inspection')
    validate_session(runner, state, binding, attempt)
    observed = state['agentMap']
    state['agentMapReadback'] = independent_map_readback(runner, state, observed)
    path = attempt / 'external-map.json'
    if path.exists():
        if runner.read_json(path) != observed:
            raise runner.Blocked('retained external map handoff changed')
    else:
        runner.private_json(path, observed, new=True)
    state['stages']['agentMap'] = 'pass'
    state['stages'].setdefault('agentPublication', 'awaiting-real-human-proposer')
    state.pop('blocker', None)
    runner.private_json(attempt / 'receipt.json', state)
    print('Original external map receipt independently observed; no model or mutation was replayed.')


def _begin_fresh_observation(runner, state, attempt, stage):
    """A retained prior pass is history, never the result of a fresh observation."""
    if state.get('oneExternalAgentConversationQualified'):
        historical = {
            'status': state.get('status'),
            'qualificationScope': state.get('qualificationScope'),
            'audienceWitness': state.get('externalAudienceWitness'),
            'resumeTranscriptSha256': state.get('agentAudienceResume', {}).get('transcriptSha256'),
        }
        history = state.get('priorExternalQualifications', [])
        if not history or history[-1] != historical:
            history = [*history, historical]
        state['priorExternalQualifications'] = history[-8:]
    state['oneExternalAgentConversationQualified'] = False
    state['status'] = 'awaiting-fresh-external-observation'
    for key in ('qualificationScope', 'externalAudienceWitness', 'externalAudience', 'agentAudienceResume'):
        state.pop(key, None)
    if state['stages'].get('agentPublication') == 'pass':
        state['stages']['agentPublication'] = 'awaiting-separate-human-approval'
    state['stages']['agentAudience'] = 'awaiting-fresh-observation'
    state['stages'][stage] = 'observing'
    if stage == 'agentMap':
        state.pop('agentMapReadback', None)
    runner.private_json(attempt / 'receipt.json', state)


def _fresh_observation(runner, args, state, binding, attempt, stage, observe):
    _begin_fresh_observation(runner, state, attempt, stage)
    try:
        return observe(runner, args, state, binding, attempt)
    except Exception as exc:
        state['oneExternalAgentConversationQualified'] = False
        state['status'] = 'blocked-fresh-external-observation'
        state['stages'][stage] = 'blocked-read-only-observation'
        state['stages']['agentAudience'] = 'blocked-read-only-observation'
        state.pop('qualificationScope', None)
        state['blocker'] = str(exc)
        runner.private_json(attempt / 'receipt.json', state)
        raise


def phase_audience(runner, args, state, binding, attempt):
    return _fresh_observation(runner, args, state, binding, attempt, 'agentAudience', _phase_audience)


def phase_observe_map(runner, args, state, binding, attempt):
    return _fresh_observation(runner, args, state, binding, attempt, 'agentMap', _phase_observe_map)
