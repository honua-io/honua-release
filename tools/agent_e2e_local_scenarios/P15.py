"""Private sharing on the candidate's published route, with a non-admin reader.

The controller seeds a saved map, never a publication. The model chooses the
publication route; a sealed action is only a locator. Fresh authenticated and
anonymous reads must corroborate the exact seeded version. Tenant isolation and
worker replacement retain the runner's preregistered exclusions. Source reads
expect typed fixture attributes; this case depends on honua-server #6815.
"""
from __future__ import annotations

from agent_e2e_local_api import Observation, Scenario
from agent_e2e_local_expectations import load_roles
from agent_e2e_p10_observer import source_path
from agent_e2e_scenario_operations import data
from agent_e2e_studio_observer import route_path
from agent_e2e_local_projections_w2g4 import seed_saved_map
TOOL = 'honua_studio_propose_publication'


class P15(Scenario):
    id = 'P15'
    supported_variants = ('A', 'B')

    def agent_permissions(self, variant):
        # Sharing is the task here; approval separation is evaluated by P14/P17.
        return ['admin:*']

    def seed(self, stack, variant):
        self.expected(variant)
        return seed_saved_map(stack, variant, namespace_prefix='p15')

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        rec, state = stack.extra['recorder'], seeded.state
        if state['variant'] != variant:
            raise ValueError('variant differs from original seed')
        rec.json('p15-source-after', 'GET', source_path(state['shelters']))
        rec.json('p15-bootstrap-probe', 'GET', '/api/v1/admin/api-keys/', principal='bootstrap')
        operator_id = rec.principals['operator'][1]
        grants = data(rec.json('p15-operator-grants', 'GET',
            '/api/v1/admin/api-keys/' + operator_id + '/effective-permissions'))
        if (grants.get('id') != operator_id or grants.get('permissions') != ['read:*']
                or grants.get('status') != 'active' or grants.get('canAuthenticate') is not True):
            raise ValueError('operator is not an active, independent, non-admin read-only principal')
        selected = [a for a in rec.actions() if a.kind == 'tool_call' and a.tool == TOOL
                    and isinstance(a.arguments, dict)
                    and all(a.arguments.get(k) == state[k] for k in ('itemId', 'versionId', 'contentHash'))]
        if selected:
            # No publication mutation is replayed by the controller.
            path = route_path(selected[-1].arguments.get('route'))
            answer = rec.json('p15-operator', 'GET', path, principal='operator')
            if answer['observation']['httpStatus'] == 200:
                artifact = data(answer)
                if (any(artifact.get(k) != state[k] for k in ('itemId', 'versionId', 'contentHash'))
                        or artifact.get('visibility') != 'team'):
                    raise ValueError('published route is not the exact team-visible saved version')
            rec.json('p15-anonymous', 'GET', path, principal='anonymous')
        return [Observation(a, None, {'projection': 'extract/v1'}) for a in self.expected(variant)]

    def handoff(self, stack, seeded, variant, run=None):
        # Status words unavailable in a native response are not invented. The
        # final-answer checker can still check names/counts and fail unsupported claims.
        if 'p15-operator' not in stack.extra['recorder'].records:
            return None
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'names': {'list': [{'body': 'p15-operator', 'pointer': '/data/packageKey'}]}}}}}


scenario = P15()
