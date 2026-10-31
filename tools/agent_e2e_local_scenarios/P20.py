"""Clarify ambiguity: provision two same-named, separately owned maps.

Blocked until the runner supplies the customer's stable-ID handoff and proves
which topics were asked and that publication followed both replies. The current
session driver assigns topics to arbitrary questions and cannot bind reply IDs
or action ordering in frozen expectations. Source data depends on server #6815.
"""
from __future__ import annotations

import copy
from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles
from agent_e2e_local_projections_w2g4 import seed_saved_map
from agent_e2e_local_scenarios.P15 import scenario as private_map
from agent_e2e_scenario_operations import data
from agent_e2e_studio_observer import guid


class P20(Scenario):
    id = 'P20'
    supported_variants = ()

    def agent_permissions(self, variant):
        return ['admin:*']

    def seed(self, stack, variant):
        self.expected(variant)
        cache = stack.extra.get('p20Seed')
        if cache is not None:
            if cache['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(copy.deepcopy(cache))
        if stack.extra.get('p20SeedStarted'):
            raise ValueError('incomplete seed; reconcile the original case without retry')
        stack.extra['p20SeedStarted'] = True
        selected = seed_saved_map(stack, variant, package='shelters-map', namespace_prefix='p20')
        rec = stack.extra['recorder']
        package = selected.state['packageKey']
        initial = rec.records['p15-map-saved'].json['data']
        envelope = initial.get('envelope')
        if not isinstance(envelope, dict) or initial.get('packageKey') != package:
            raise ValueError('seeded map did not expose its native package and envelope')
        other_owner = stack.extra['addPrincipal']('otherteam', ['read:*'])
        if other_owner == rec.principals['agent'][1]:
            raise ValueError('the other map must have a separate owner')
        draft = data(rec.json('p20-other-draft', 'POST', '/api/v1/studio/package-drafts', {
            'packageKey': package, 'workspaceId': 'other-' + stack.compose_project[-24:],
            'ownerId': other_owner, 'envelope': envelope}), (201,))
        other = data(rec.json('p20-other-saved', 'POST', '/api/v1/studio/package-drafts/'
                              + guid(draft['draftId']) + '/content-versions', {}), (201,))
        other_id = guid(other['itemId'])
        if other_id == selected.state['itemId'] or other.get('packageKey') != package or other.get('ownerId') != other_owner:
            raise ValueError('same-named map fixture did not preserve distinct native owners and items')
        state = {**selected.state, 'otherItemId': other_id, 'otherVersionId': guid(other['versionId'])}
        stack.extra['p20Seed'] = copy.deepcopy(state)
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        observations = private_map.observe(stack, seeded, variant, run)
        private = next(item for item in observations if item.assertion_id == 'publication.private')
        return [Observation('clarification.required', None, {'projection': 'extract/v1'}), private]


scenario = P20()
