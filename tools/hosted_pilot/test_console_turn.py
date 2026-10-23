"""Original Console input contracts; local doubles do not qualify human custody."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import console_turn
from agent_e2e_pack import canonical_digest, digest_bytes, load_json, select
from browser_session import operator_partition
import test_prepare as preparation_tests


class ConsoleTurnAdoption(unittest.TestCase):
    context_setup = preparation_tests.PreparationBoundary.context_setup
    def adopted(self):
        config, durable, client, workspace = self.context_setup()
        workspace['revision'] = 4
        workspace['selectedResources'] = [{'kind': 'layer', 'id': 'actual-selected-layer'}]
        pack_path = Path(__file__).resolve().parents[2]/'certification/agent-e2e/pack.v2.json'
        pack = {'path': 'certification/agent-e2e/pack.v2.json', 'sha256': digest_bytes(pack_path.read_bytes())}
        case_id = 'P14:A:aws-ecs-redis-on'
        case, scenario = select(load_json(pack_path), case_id)
        prompt = scenario['prompts'][case['variant']]
        self.human_target['principal']['subjectId'] = 'actual-subject'
        turn = {'turnId': config['turnId'], 'status': 'accepted', 'context': copy.deepcopy(workspace),
            'originatingActorId': self.human_target['principal']['actorId'], 'messages': [
                {'id': config['turnId']+'-user', 'turnId': config['turnId'], 'role': 'user', 'content': prompt, 'evidenceKind': 'conversation'}],
            'browserFileSelection': {'selectionId': 'actual-human-selection', 'fileName': 'original.geojson',
                'totalBytes': 42, 'expectedSha256': 'a'*64}}
        sources = {name: turn.get(name) for name in ('draft', 'localProcessSnapshot', 'datasetImportContext', 'browserFileSelection')}
        profile = {'id': 'actual-profile', 'serverUri': config['endpoint']}
        observed = {'session': {'authenticated': True, 'principalId': operator_partition('actual-subject', profile), 'environmentId': profile['id']},
            'nativeTarget': copy.deepcopy(self.human_target), 'consoleProfile': profile, 'deployment': {'actual': 'deployment'}, 'expiresAt': 'future'}
        custody = {'schema': 'honua.private-console-human-session/v1', 'producer': 'private-console-human-capture',
            'candidateKey': durable.store.candidate, 'configuration': {'consoleProfile': profile}, 'observed': observed, 'qualification': False}
        reference = {'key': 'actual-console-capture', 'sha256': canonical_digest(custody)}
        durable.state['scenarioOperations'][reference['key']] = custody
        config['consoleTurn'] = {'workspaceId': workspace['workspaceId'], 'pack': pack, 'caseId': case_id, 'sessionCustody': reference,
            'contextSha256': canonical_digest(workspace), 'sourceSha256': canonical_digest(sources)}
        def answers(values):
            return [{'observation': {'httpStatus': 200, 'body': value}} for value in values]
        client.json.side_effect = answers((self.human_target, workspace, turn))
        self.agent.json.side_effect = answers((self.agent_target, workspace, turn))
        return config, durable, client, workspace, turn, observed

    def run_adoption(self, config, durable, observed):
        import prepare
        import os
        with patch('prepare.BootstrapHttp', side_effect=self.factory), patch('prepare.verify_injected_secret'), \
                patch('console_turn.observe_session', return_value=observed), \
                patch.dict(os.environ, {'PILOT_CONTROLLER_TASK_ARN': 'actual-task'}):
            return prepare.native_context(durable, config)

    def test_adopts_original_human_turn_using_only_independent_gets(self):
        config, durable, client, workspace, turn, observed = self.adopted()
        result = self.run_adoption(config, durable, observed)
        self.assertEqual(result['value']['turn'], turn)
        self.assertEqual(result['value']['workspace']['revision'], 4)
        self.assertEqual(result['value']['consoleTurn']['sessionObservation'], observed)
        self.assertEqual([call.args[1] for call in client.json.call_args_list], ['GET']*3)
        self.assertEqual([call.args[1] for call in self.agent.json.call_args_list], ['GET']*3)
        self.assertEqual(console_turn.retained_session(durable, config['consoleTurn']['sessionCustody'], config['endpoint'],
            self.human_target['principal'])['observed'], observed)
        self.assertFalse(result['qualification'])

    def test_actual_author_prompt_context_source_and_existing_response_are_not_inferred(self):
        changes = [lambda t: t.pop('originatingActorId'), lambda t: t.update(originatingActorId='other-human'),
            lambda t: t['messages'][0].update(content='easier task'), lambda t: t['context'].update(revision=5),
            lambda t: t['browserFileSelection'].update(totalBytes=43),
            lambda t: t['messages'].append({'id': 'previous-output', 'turnId': t['turnId'], 'role': 'assistant', 'content': 'Already ran'})]
        for change in changes:
            with self.subTest(change=changes.index(change)):
                config, durable, client, _, turn, observed = self.adopted()
                change(turn)
                with self.assertRaises(ValueError):
                    self.run_adoption(config, durable, observed)
                self.assertFalse(any(key.endswith('/completed') for key in durable.state['studioBootstrapOperations']))
                self.assertTrue(all(call.args[1] == 'GET' for call in client.json.call_args_list))
                self.assertEqual(self.agent.json.call_count, 1)

    def test_retained_human_partition_endpoint_or_subject_cannot_be_substituted(self):
        for field in ('subjectId', 'actorId', 'isHumanSession'):
            config, durable, _, _, _, _ = self.adopted()
            human = copy.deepcopy(self.human_target['principal']); human[field] = 'substituted'
            with self.subTest(field=field), self.assertRaises(ValueError):
                console_turn.retained_session(durable, config['consoleTurn']['sessionCustody'], config['endpoint'], human)
        config, durable, _, _, _, _ = self.adopted()
        with self.assertRaises(ValueError):
            console_turn.retained_session(durable, config['consoleTurn']['sessionCustody'], 'https://api.pilot.demo.honua.io', self.human_target['principal'])

    def test_corrupted_frozen_pack_refuses_before_any_native_call(self):
        config, durable, client, _, _, observed = self.adopted()
        config['consoleTurn']['pack']['sha256'] = 'sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError, 'pack bytes differ'):
            self.run_adoption(config, durable, observed)
        client.json.assert_not_called()
