"""Offline fail-closed evidence joins only; never a live model/browser qualification."""
import copy
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('native_map_run', Path(__file__).with_name('run.py'))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
helper = runner.agent_map


class AgentMapTests(unittest.TestCase):
    def setUp(self):
        self.original = {'workspaceId': 'w1', 'turnId': 'import-turn', 'targetId': 'target1', 'tenantId': 'tenant1'}
        self.service = {'turnId': 'service-turn', 'studioSourceId': 'source1'}
        self.task = {'id': 'draft1', 'kind': 'studio-composition', 'revision': 1, 'packageKey': 'map1'}
        self.context = {**{k: self.original[k] for k in ('workspaceId', 'targetId', 'tenantId')},
                        'viewId': 'view1', 'revision': 9, 'task': self.task}
        self.messages = [{'role': 'user', 'turnId': 'map-turn', 'content': helper.MAP_PROMPT}]
        self.before = {'draftId': 'draft1', 'itemId': 'item1', 'packageKey': 'map1', 'workspaceId': 'w1',
                       'family': 'map', 'generation': 1, 'envelope': {'body': {'layers': []}}}
        added = copy.deepcopy(self.before)
        added['generation'] = 2
        self.layer = {'id': 'presentation1', 'sourceId': 'source1', 'type': 'line'}
        added['envelope']['body']['layers'] = [self.layer]
        self.style = {'presentation1': {'paint': {'line-color': '#2563eb'}}}
        styled = copy.deepcopy(added)
        styled['generation'] = 3
        styled['envelope']['body']['styleRefs'] = [{'styleId': 'honua-inline:presentation1', 'body': self.style}]
        self.version = {'itemId': 'item1', 'versionId': 'version1', 'contentHash': 'a' * 64,
                        'envelope': copy.deepcopy(styled['envelope'])}
        self.reopened = {**copy.deepcopy(styled), 'draftId': 'draft2', 'generation': 1, 'baseVersionId': 'version1',
                         'operation': self.operation('reopen1')}
        locator = {'workspaceId': 'w1', 'turnId': 'map-turn'}
        link = {'workspaceId': 'w1', 'viewId': 'view1', 'expectedRevision': 9, 'targetId': 'target1', 'selectedResources': [],
                'task': {**self.task, 'id': 'draft2', 'revision': 1}}
        current = {**self.context, 'revision': 10, 'task': link['task']}
        self.exchanges = [
            ('honua_workspace_conversation', {'workspaceId': 'w1'}, {'messages': self.messages}),
            ('honua_workspace_get_turn', locator, {'turnId': 'map-turn', 'messages': self.messages, 'context': self.context}),
            ('honua_agent_connect', locator, {'state': 'attached', 'turnBinding': locator}),
            ('honua_studio_get_draft', {'draftId': 'draft1'}, self.before),
            ('honua_studio_add_layer', {'draftId': 'draft1', 'generation': 1, 'layer': self.layer}, added),
            ('honua_studio_set_layer_style', {'draftId': 'draft1', 'generation': 2, 'layerId': 'presentation1', 'styleBody': self.style}, styled),
            ('honua_studio_save_version', {'draftId': 'draft1', 'generation': 3}, {'operation': self.operation('save1'), 'version': self.version}),
            ('honua_studio_get_version', {'itemId': 'item1', 'versionId': 'version1'}, {'versionId': 'version1', 'contentHash': 'a' * 64}),
            ('honua_studio_reopen_version', {'itemId': 'item1', 'versionId': 'version1'}, self.reopened),
            ('honua_studio_get_draft', {'draftId': 'draft2'}, self.reopened),
            ('honua_workspace_set_context', link, current),
            ('honua_workspace_get', {'workspaceId': 'w1', 'viewId': 'view1'}, current),
            ('honua_workspace_append_message', {'workspaceId':'w1','turnId':'map-turn','messageId':'summary1','content':helper.MAP_SUMMARY},
             {'id':'summary1','turnId':'map-turn','role':'assistant','content':helper.MAP_SUMMARY,'evidenceKind':'conversation'}),
        ]

    @staticmethod
    def operation(identifier):
        return {'operationInstanceId': identifier, 'status': 'Completed', 'requiresApproval': False}

    @staticmethod
    def encode(exchanges, structured=True):
        events = []
        for index, (tool, arguments, value) in enumerate(exchanges):
            call = 'call' + str(index)
            events.append({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': call,
                            'name': 'mcp__honua__' + tool, 'input': arguments}]}})
            event = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': call,
                     'content': json.dumps(value)}]}}
            if structured:
                event['tool_use_result'] = {'structuredContent': value}
            events.append(event)
        return '\n'.join(json.dumps(e) for e in events)

    def observe(self, exchanges=None):
        return helper.observed_map(runner, self.encode(self.exchanges if exchanges is None else exchanges),
                                   self.service, self.original, 'map-turn')

    def test_joins_existing_human_map_generation_source_save_reopen_and_workspace_pointer(self):
        receipt = self.observe()
        self.assertEqual(receipt['reopenedDraftId'], 'draft2')
        self.assertEqual(receipt['operationInstanceIds'], ['save1', 'reopen1'])
        self.assertEqual(receipt['bodyCanonicalSha256'], helper.body_hash(self.version['envelope']['body']))
        self.assertFalse(receipt['renderedMapVerified'])
        self.assertEqual(receipt['publication'], 'awaiting-real-human-proposer')

    def test_model_prose_and_unrelated_or_repeated_mutations_cannot_pass(self):
        with self.assertRaises(runner.Blocked):
            helper.observed_map(runner, self.encode(self.exchanges, structured=False), self.service, self.original, 'map-turn')
        for tool in ('honua_studio_propose_publication', 'honua_admin_proposals_approve', 'honua_studio_add_layer'):
            broken = copy.deepcopy(self.exchanges) + [(tool, {}, {})]
            with self.subTest(tool=tool), self.assertRaises(runner.Blocked):
                self.observe(broken)

    def test_rejects_context_generation_source_style_and_saved_version_drift(self):
        for index, section, key, value in ((1, 'output', 'turnId', 'another-turn'),
                                         (4, 'input', 'generation', 2), (5, 'input', 'layerId', 'other'),
                                         (7, 'output', 'contentHash', 'b' * 64),
                                         (8, 'input', 'versionId', 'other-version'),
                                         (9, 'output', 'draftId', 'replacement')):
            broken = copy.deepcopy(self.exchanges)
            target = broken[index][1 if section == 'input' else 2]
            target[key] = value
            with self.subTest(index=index, key=key), self.assertRaises(runner.Blocked):
                self.observe(broken)
        broken = copy.deepcopy(self.exchanges)
        broken[4][1]['layer']['sourceId'] = 'other-source'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)
        broken = copy.deepcopy(self.exchanges)
        broken[6][2]['operation']['status'] = 'AwaitingApproval'
        with self.assertRaises(runner.Blocked):
            self.observe(broken)

    def test_independent_http_reads_must_match_all_draft_version_content_identities(self):
        receipt = self.observe()
        original_draft = {**self.before, 'envelope': copy.deepcopy(self.version['envelope'])}
        documents = [self.version, original_draft, self.reopened]
        from unittest.mock import Mock
        read = Mock(side_effect=copy.deepcopy(documents))
        result = helper.independent_map_readback(runner, {}, receipt, read=read)
        self.assertEqual(result['status'], 'pass')
        self.assertFalse(result['renderedMapVerified'])
        for index in range(3):
            broken = copy.deepcopy(documents)
            broken[index]['envelope']['body']['layers'][0]['sourceId'] = 'other-source'
            with self.subTest(index=index), self.assertRaises(runner.Blocked):
                helper.independent_map_readback(runner, {}, receipt, read=Mock(side_effect=broken))

    def test_audience_resumption_is_same_original_turn_and_read_only(self):
        receipt = self.observe()
        locator = {'workspaceId': 'w1', 'turnId': 'map-turn'}
        exchanges = [self.exchanges[0], self.exchanges[1], self.exchanges[2],
                     self.exchanges[9], self.exchanges[7]]
        result = helper.observed_audience_resume(runner, self.encode(exchanges), self.original, receipt)
        self.assertEqual(result['anonymousAudienceObservedBy'], 'independent-controller-browser')
        with self.assertRaises(runner.Blocked):
            helper.observed_audience_resume(runner, self.encode(exchanges + [self.exchanges[8]]), self.original, receipt)


    def test_preserves_unrelated_before_draft_content_and_rejects_independent_identity_drift(self):
        broken = copy.deepcopy(self.exchanges)
        broken[3][2]['envelope']['body']['widgets'] = [{'id': 'human-chart', 'kind': 'bar'}]
        with self.assertRaisesRegex(runner.Blocked, 'unrelated'):
            self.observe(broken)
        receipt = self.observe()
        from unittest.mock import Mock
        original_draft = {**self.before, 'envelope': copy.deepcopy(self.version['envelope'])}
        for index, key in ((0, 'itemId'), (0, 'versionId'), (0, 'contentHash'), (1, 'draftId'), (2, 'workspaceId')):
            documents = copy.deepcopy([self.version, original_draft, self.reopened])
            documents[index][key] = 'wrong'
            with self.subTest(index=index, key=key), self.assertRaises(runner.Blocked):
                helper.independent_map_readback(runner, {}, receipt, read=Mock(side_effect=documents))

    def test_map_response_loss_blocks_mutation_replay(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import subprocess
        import tempfile
        args = SimpleNamespace(workspace_id='w1', turn_id='map-turn', claude='real-client')
        state = {'stages': {'agentService': 'pass'}, 'agentImportTask': self.original,
                 'agentService': self.service, 'externalClientSessionId': 'same-session'}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(helper, 'validate_session', return_value=Path(directory) / 'mcp.json'), \
                patch.object(runner, 'private_json'), \
                patch.object(helper.subprocess, 'run', side_effect=subprocess.TimeoutExpired('real-client', 900)) as external:
            with self.assertRaises(subprocess.TimeoutExpired):
                helper.phase_map(runner, args, state, {}, Path(directory))
            self.assertEqual(state['stages']['agentMap'], 'blocked')
            with self.assertRaises(runner.Blocked):
                helper.phase_map(runner, args, state, {}, Path(directory))
            self.assertEqual(external.call_count, 1)

    def pending_handoff(self):
        observed = self.observe()
        return {**observed, 'status':'awaiting-separate-human-approval','route':'/review-map',
                'proposalId':'proposal1','requestedBy':'human1','operationInstanceId':'publish1',
                'commandId':'command1','requestId':'request1','renderedMapVerified':True,
                'canonicalProposal':{'proposalId':'proposal1','status':'AwaitingApproval','requestedBy':'human1',
                    'diff':['itemId=item1','versionId=version1','contentHash='+'a'*64,'route=/review-map','visibility=public']}}

    def pending_operation(self):
        return {'operationInstanceId':'publish1','operationId':'studio.content.create-publication-request',
                'proposalId':'proposal1','status':'RequiresApproval','resourceIds':{}}

    def test_pending_capture_joins_canonical_human_diff_and_operation_without_claiming_request_actuation(self):
        handoff = self.pending_handoff()
        result = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _:self.pending_operation())
        self.assertFalse(result['requestIdentityVerified'])
        canonical = {**self.pending_operation(), 'resourceIds': {
            'itemId': 'item1', 'versionId': 'version1', 'requestId': 'request1'}}
        joined = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _: canonical)
        self.assertEqual(joined['requestId'], 'request1')
        self.assertTrue(joined['requestIdentityVerified'])
        self.assertEqual(result['pendingBinding'],'canonical-proposal-diff-and-operation')
        for field,value in [('status','Succeeded'),('requestedByAgent','agent1'),('resolvedBy','human2'),
                            ('diff',['itemId=other']),('proposalId','other')]:
            broken=copy.deepcopy(handoff)
            broken['canonicalProposal'][field]=value
            with self.subTest(field=field),self.assertRaises(runner.Blocked):
                helper.observed_pending_publication(runner, {}, broken, self.observe(),'proposal1','/review-map',
                                                     read=lambda _:self.pending_operation())
        for field,value in [('status','Completed'),('proposalId','other'),('operationId','other'),
                            ('resourceIds',{'itemId':'wrong','versionId':'version1','requestId':'request1'})]:
            operation={**self.pending_operation(),field:value}
            with self.subTest(field=field),self.assertRaises(runner.Blocked):
                helper.observed_pending_publication(runner, {}, handoff, self.observe(),'proposal1','/review-map',
                                                     read=lambda _:operation)

    def test_pending_capture_accepts_absent_request_id_but_rejects_wrong_observed_id(self):
        handoff = self.pending_handoff()
        handoff.pop('requestId')
        result = helper.observed_pending_publication(runner, {}, handoff, self.observe(), 'proposal1', '/review-map',
                                                     read=lambda _: self.pending_operation())
        self.assertNotIn('requestId', result)
        self.assertFalse(result['requestIdentityVerified'])
        operation = {**self.pending_operation(), 'resourceIds': {
            'itemId': 'item1', 'versionId': 'version1', 'requestId': 'other'}}
        with self.assertRaises(runner.Blocked):
            helper.observed_pending_publication(runner, {}, self.pending_handoff(), self.observe(),
                                               'proposal1', '/review-map', read=lambda _: operation)

    def test_audience_requires_final_request_and_same_operation_with_optional_pending_id(self):
        handoff = self.pending_handoff()
        witness = {**handoff, 'status': 'pass', 'resolvedBy': 'human2'}
        helper.validate_audience_witness(runner, handoff, witness)
        pending_without_id = {key: value for key, value in handoff.items() if key != 'requestId'}
        helper.validate_audience_witness(runner, pending_without_id, witness)
        for field, value in [('requestId', None), ('requestId', ''), ('operationInstanceId', 'other'),
                             ('proposalId', 'other'), ('itemId', 'other'), ('versionId', 'other')]:
            with self.subTest(field=field, value=value), self.assertRaises(runner.Blocked):
                helper.validate_audience_witness(runner, pending_without_id, {**witness, field: value})
        with self.assertRaises(runner.Blocked):
            helper.validate_audience_witness(runner, handoff, {**witness, 'requestId': 'other'})

    def test_read_only_human_capture_can_retry_lost_response_without_publication(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            browser_state=root/'human.json'
            browser_state.write_text('{}')  # Test path only, never live human credentials.
            observed=self.observe()
            runner.private_json(root/'external-map.json',observed,new=True)
            args=SimpleNamespace(human_browser_cdp='http://127.0.0.1:9222',origin='http://127.0.0.1:8081',route='/review-map',
                                 console_root=root,proposal_id='proposal1')
            state={'stages':{'agentMap':'pass','agentPublication':'awaiting-real-human-proposer'},
                   'target':{'origins':{'console':args.origin}},'agentMap':observed}
            with patch.object(helper,'validate_session'),patch.object(helper,'independent_map_readback'), \
                    patch.object(helper.subprocess,'run',return_value=SimpleNamespace(returncode=1,stdout='',stderr='lost capture')) as browser:
                for _ in range(2):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_publication(runner,args,state,{},root)
                self.assertEqual(browser.call_count,2)
                self.assertEqual(state['stages']['agentPublication'],'blocked-read-only-capture')
            def capture(command,**kwargs):
                runner.private_json(Path(command[6]),self.pending_handoff(),new=True)
                return SimpleNamespace(returncode=0,stdout='',stderr='')
            with patch.object(helper,'validate_session'),patch.object(helper,'independent_map_readback'), \
                    patch.object(helper,'server_reader',return_value=lambda _:self.pending_operation()), \
                    patch.object(helper.subprocess,'run',side_effect=capture):
                helper.phase_publication(runner,args,state,{},root)
            self.assertEqual(state['stages']['agentPublication'],'awaiting-separate-human-approval')
            self.assertFalse(state['externalPublication']['requestIdentityVerified'])

    def test_missing_exact_pending_proposal_blocks_before_browser_and_helper_has_no_mutation_controls(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            browser_state=root/'human.json'
            browser_state.write_text('{}')
            args=SimpleNamespace(human_browser_cdp='http://127.0.0.1:9222',origin='http://127.0.0.1:8081',route='/review-map',proposal_id=None)
            state={'stages':{'agentMap':'pass','agentPublication':'awaiting-real-human-proposer'},
                   'target':{'origins':{'console':args.origin}},'agentMap':self.observe()}
            with patch.object(helper,'validate_session'),patch.object(helper.subprocess,'run') as browser:
                with self.assertRaises(runner.Blocked):
                    helper.phase_publication(runner,args,state,{},root)
                browser.assert_not_called()
        source=Path(__file__).with_name('external_map_publication.mjs').read_text()
        for forbidden in ('.click(','.fill(','.post(','.put(','.delete('):
            self.assertNotIn(forbidden,source)
        for forbidden in ('chromium.launch(','.newContext(','.newPage(','.goto(','.route(','.close('):
            self.assertNotIn(forbidden,source)
        self.assertIn('chromium.connectOverCDP(humanBrowserCdp)',source)
        self.assertIn('process.exit(exitStatus)',source)

    def test_human_browser_cdp_is_explicit_loopback_origin_only(self):
        self.assertTrue(helper.valid_human_browser_cdp('http://127.0.0.1:9222'))
        for value in (None,'','https://127.0.0.1:9222','http://remote:9222','http://127.0.0.1:0',
                      'http://127.0.0.1:65536','http://user@127.0.0.1:9222','http://127.0.0.1:9222/path',
                      'http://127.0.0.1:9222?token=secret','http://127.0.0.1:9222#fragment'):
            with self.subTest(value=value):
                self.assertFalse(helper.valid_human_browser_cdp(value))

    def test_external_session_mismatch_cannot_qualify_and_audience_turn_is_frozen(self):
        with self.assertRaises(runner.Blocked):
            runner.observed_client_session(json.dumps({'type':'system','subtype':'init','session_id':'other'}), 'same')
        receipt = self.observe()
        changed = copy.deepcopy([self.exchanges[0], self.exchanges[1], self.exchanges[2], self.exchanges[9], self.exchanges[7]])
        changed[1][2]['context']['targetId'] = 'other-target'
        with self.assertRaises(runner.Blocked):
            helper.observed_audience_resume(runner, self.encode(changed), self.original, receipt)


    def test_requalification_failure_cannot_retain_previous_pass(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        import tempfile
        for failure in ('witness', 'resume', 'readback', 'context'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                observed, handoff = self.observe(), self.pending_handoff()
                runner.private_json(root / 'external-publication-handoff.json', handoff)
                (root / '.env').write_text('HONUA_AGENT_API_KEY=synthetic-offline-key\n')
                operator = root / 'operator.json'; operator.write_text('{}')
                witness_ref = {'path': 'previous-witness.json', 'sha256': 'a' * 64}
                runner.private_json(root / witness_ref['path'], {'offline': True})
                state = {'stages': {key: 'pass' for key in ('prepare', 'install', 'agentTask', 'agentSubmission',
                         'agentImportObservation', 'agentService', 'agentMap', 'agentPublication', 'agentAudience')},
                         'agentImportTask': self.original, 'agentMap': observed, 'externalPublication': handoff,
                         'target': {'origins': {'console': 'http://127.0.0.1:8081', 'server': 'http://127.0.0.1:8080'}},
                         'installDirectory': str(root), 'externalClientSessionId': 'same-session',
                         'status': 'same-external-agent-with-human-governance-pass',
                         'oneExternalAgentConversationQualified': True, 'qualificationScope': 'previous observed scope',
                         'externalAudienceWitness': witness_ref,
                         'agentAudienceResume': {'transcriptSha256': 'b' * 64}, 'externalAudience': {'status': 'pass'}}
                args = SimpleNamespace(operator_state=operator, origin='http://127.0.0.1:8081',
                         server_origin='http://127.0.0.1:8080', console_root=root, claude='offline-only')
                def execute(command, **kwargs):
                    if 'env' in kwargs:
                        if failure == 'witness': return SimpleNamespace(returncode=1, stdout='', stderr='failed witness')
                        witness = {**handoff, 'status': 'pass', 'resolvedBy': 'human2'}
                        runner.private_json(Path(kwargs['env']['HONUA_WORKSPACE_E2E_PUBLICATION_WITNESS']), witness, new=True)
                        return SimpleNamespace(returncode=0, stdout='', stderr='')
                    return SimpleNamespace(returncode=1 if failure == 'resume' else 0, stdout='', stderr='')
                with patch.object(helper, 'validate_session', side_effect=runner.Blocked('context changed') if failure == 'context' else None), \
                        patch.object(runner, 'playwright_command', return_value=['offline-witness']), \
                        patch.object(helper.subprocess, 'run', side_effect=execute), \
                        patch.object(runner, 'observed_client_session'), \
                        patch.object(helper, 'observed_audience_resume', return_value={'status': 'pass'}), \
                        patch.object(helper, 'independent_map_readback', side_effect=runner.Blocked('readback changed') if failure == 'readback' else None):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_audience(runner, args, state, {}, root)
                retained = runner.read_json(root / 'receipt.json')
                self.assertFalse(retained['oneExternalAgentConversationQualified'])
                self.assertEqual(retained['status'], 'blocked-fresh-external-observation')
                self.assertEqual(retained['stages']['agentAudience'], 'blocked-read-only-observation')
                self.assertNotIn('qualificationScope', retained)
                self.assertEqual(retained['priorExternalQualifications'][0]['audienceWitness'], witness_ref)
                self.assertTrue((root / witness_ref['path']).exists())

    def test_failed_map_reobservation_does_not_resurrect_prior_qualification(self):
        from unittest.mock import patch
        import tempfile
        for failure in ('context', 'readback'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state = {'stages': {'agentMap': 'pass', 'agentPublication': 'pass', 'agentAudience': 'pass'},
                         'agentMap': self.observe(), 'oneExternalAgentConversationQualified': True,
                         'status': 'same-external-agent-with-human-governance-pass',
                         'qualificationScope': 'prior', 'externalAudienceWitness': {'path': 'old', 'sha256': 'a'*64}}
                with patch.object(helper, 'validate_session', side_effect=runner.Blocked('context changed') if failure == 'context' else None), \
                        patch.object(helper, 'independent_map_readback', side_effect=runner.Blocked('readback failed')):
                    with self.assertRaises(runner.Blocked):
                        helper.phase_observe_map(runner, None, state, {}, root)
                self.assertFalse(runner.read_json(root / 'receipt.json')['oneExternalAgentConversationQualified'])
                self.assertEqual(state['stages']['agentMap'], 'blocked-read-only-observation')
                self.assertNotEqual(state['stages']['agentPublication'], 'pass')

    def test_successful_map_only_reobservation_still_requires_fresh_audience(self):
        from unittest.mock import patch
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = {'stages': {'agentMap': 'pass', 'agentPublication': 'pass', 'agentAudience': 'pass'},
                     'agentMap': self.observe(), 'oneExternalAgentConversationQualified': True,
                     'status': 'same-external-agent-with-human-governance-pass', 'qualificationScope': 'prior'}
            with patch.object(helper, 'validate_session'), patch.object(helper, 'independent_map_readback', return_value={'status': 'pass'}):
                helper.phase_observe_map(runner, None, state, {}, root)
            self.assertEqual(state['stages']['agentMap'], 'pass')
            self.assertFalse(state['oneExternalAgentConversationQualified'])
            self.assertEqual(state['stages']['agentAudience'], 'awaiting-fresh-observation')
            self.assertEqual(len(state['priorExternalQualifications']), 1)


if __name__ == '__main__':
    unittest.main()
