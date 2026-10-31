"""P10 filter boundaries: the reference scenario for the local corpus runner (API v1.1).

seed     publish the frozen town (published-town) through the runner recorder and read
         the shelters layer once (seed-phase baseline)
observe  replay the model's own last honua_query_features call on the seeded shelters
         layer from the digest-verified action log, with controller credentials, and
         read the shelters layer again (observe-phase)
score    the runner projects the replay response to its source_id set (filter.exact) and
         the before/after layer reads to protected content (source-content.unchanged)

Expectations: certification/agent-e2e/expectations/local/P10.json (fixture-derived).
The scenario never supplies an observed value or a response body.
"""
from __future__ import annotations

from pathlib import Path

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]
QUERY_TOOL = 'honua_query_features'
SERVICE_ROLE, LAYER_ROLE = 'seeded:service:shelters', 'seeded:layer:shelters'
# Where a completed service.publish handle names the published service and layer.
PUBLISH_STEP = 'publish-shelters-submit'
SERVICE_POINTER, LAYER_POINTER = '/data/resourceIds/serviceName', '/data/resourceIds/layerId'


class P10(Scenario):
    id = 'P10'
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        from agent_e2e_scenario_backend import TownSetup
        from agent_e2e_p10_observer import source_path
        if variant not in self.supported_variants:
            raise ValueError('unsupported P10 variant')
        if 'p10Seed' in stack.extra:
            if stack.extra['p10Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p10Seed'])
        recorder = stack.extra['recorder']
        namespace = 'p10_' + stack.compose_project.replace('-', '')[-16:]
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        town = TownSetup(recorder, ROOT / 'certification/agent-e2e/corpus/manifest.v1.json').prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        binding = town['layers']['shelters']
        recorder.json('p10-source-before', 'GET', source_path(binding))
        seed = {'variant': variant, 'service': binding['service'], 'layerId': binding['layerId']}
        stack.extra['p10Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding(SERVICE_ROLE, PUBLISH_STEP, pointer=SERVICE_POINTER),
                RoleBinding(LAYER_ROLE, PUBLISH_STEP, pointer=LAYER_POINTER)]

    @staticmethod
    def selected_query(actions, seeded):
        """The model's last query of the seeded shelters layer, from the verified action log."""
        chosen = None
        for action in actions:
            arguments = action.arguments if isinstance(action.arguments, dict) else {}
            if (action.kind == 'tool_call' and action.tool == QUERY_TOOL
                    and str(arguments.get('serviceId')) == str(seeded.state['service'])
                    and str(arguments.get('layerId')) == str(seeded.state['layerId'])):
                chosen = action
        return chosen

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_p10_observer import source_path
        recorder = stack.extra['recorder']
        binding = {'service': seeded.state['service'], 'layerId': seeded.state['layerId']}
        recorder.json('p10-source-after', 'GET', source_path(binding))
        query = self.selected_query(recorder.actions(), seeded)
        if query is not None:
            try:
                recorder.replay('p10-replay-query', query)
            except Exception:  # the recorded response (if any) is still what gets scored
                pass
        service, layer = {'role': SERVICE_ROLE}, {'role': LAYER_ROLE}
        return [
            Observation('filter.exact', None, {'projection': 'extract/v1', 'spec': {'object': {'resourceIds': {
                'featureQueryIds': 'p10-replay-query', 'service': service, 'layer': layer}}}}),
            Observation('source-content.unchanged', None, {'projection': 'extract/v1', 'spec': {'protectedContent': [
                {'label': 'shelters', 'before': 'p10-source-before', 'after': 'p10-source-after',
                 'resource': [SERVICE_ROLE, LAYER_ROLE]}]}}),
        ]

    def handoff(self, stack, seeded, variant, run=None):
        """Controller facts the customer's final answer is checked against."""
        if 'p10-replay-query' not in stack.extra['recorder'].records:
            return None
        features = {'body': 'p10-replay-query', 'pointer': '/result/structuredContent/geojson/features'}
        count = {'count': features}
        return {'object': {'resources': {'list': []},
                           'facts': {'object': {'counts': {'object': {'features': count, 'points': count}},
                                                'names': {'map': features, 'to': {'item': '/properties/name'}}}}}}


scenario = P10()
