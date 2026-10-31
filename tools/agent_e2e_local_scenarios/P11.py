"""P11 remains unsupported until local job-to-output and saved-map evidence is bound.

The runner needs a post-seed agent-owned job locator, a frozen projection for
the persisted buffer artifact and an independently verified map/output binding.
The frozen town feature projections use typed attributes, so they depend on
honua-server #6815. A fixture buffer alone cannot prove the requested saved result was made.
"""
from pathlib import Path
import re
from urllib.parse import quote

from agent_e2e_local_api import Observation, RoleBinding, Scenario, Seeded

ROOT = Path(__file__).resolve().parents[2]


class P11(Scenario):
    id = 'P11'
    supported_variants = ()

    def seed(self, stack, variant):
        from agent_e2e_local_scenarios.w2g3_common import read_town, town_setup
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        if 'p11Seed' in stack.extra:
            if stack.extra['p11Seed']['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(stack.extra['p11Seed'])
        recorder = stack.extra['recorder']
        namespace = 'p11_' + stack.compose_project.replace('-', '')[-16:]
        if 'connectionId' not in stack.extra:
            stack.extra['connectionId'] = stack.extra['ensureConnection']()
        town = town_setup(recorder, variant).prepare(
            variant=variant, namespace=namespace, starting_state='published-town',
            connection_id=stack.extra['connectionId'])
        binding = town['layers']['shelters']
        read_town(recorder, 'p11', town['layers'], 'before')
        recorder.json('p11-jobs-before', 'GET', '/api/v1/admin/jobs?limit=100')
        seed = {'variant': variant, 'service': binding['service'], 'layerId': binding['layerId'], 'layers': town['layers']}
        stack.extra['p11Seed'] = seed
        return Seeded(seed)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding('created:job:buffer', 'p11-jobs-after', items='/items', id='/jobId',
                            before='p11-jobs-before', next_page='/nextCursor',
                            created_by={'tool':'honua_execute_plan', 'pointer':'/result/structuredContent/jobId'}),
                *[RoleBinding(f'seeded:{kind}:{layer}', f'publish-{layer.replace("_", "-")}-submit',
                            pointer=f'/data/resourceIds/{field}')
                for layer in ('parcels', 'flood_zones', 'shelters')
                for kind, field in (('service', 'serviceName'), ('layer', 'layerId'))]]

    def observe(self, stack, seeded, variant, run=None):
        from agent_e2e_local_scenarios.w2g3_common import read_town
        recorder = stack.extra['recorder']
        read_town(recorder, 'p11', seeded.state['layers'], 'after')
        listing = recorder.json('p11-jobs-after', 'GET', '/api/v1/admin/jobs?limit=100')
        live = listing['observation']['body']
        candidates = []
        for index, action in enumerate(recorder.actions()):
            if action.kind != 'tool_call' or action.tool != 'honua_execute_plan' or action.status != 'pass':
                continue
            response = recorder.action_result(f'p11-action-{index}', action)
            value = response.json.get('result', {}).get('structuredContent', {})
            job = value.get('jobId') if isinstance(value, dict) else None
            if isinstance(job, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,200}', job):
                if any(row.get('jobId') == job for row in live.get('items', [])):
                    candidates.append((job, action))
        if len(candidates) == 1:
            job, action = candidates[0]
            recorder.action_result('p11-job-action', action)
            recorder.json('p11-job-detail', 'GET', '/api/v1/admin/jobs/' + quote(job, safe=''))
        return [Observation('job.completed', None, {'projection': 'extract/v1'}),
                Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


scenario = P11()
