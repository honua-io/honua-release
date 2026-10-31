"""Key lifecycle: seed the service and retain bootstrap custody evidence.

Blocked until the runner can safely bind the same model-issued credential to
read/write and post-revocation probes, and bind the complete secret-scan
inventory. API-key listing is intentionally not used as an inventory.
"""
from __future__ import annotations

import copy
import re

from agent_e2e_local_api import Scenario, Seeded
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings, seed_town


class P16(Scenario):
    id = 'P16'
    supported_variants = ()

    def agent_permissions(self, variant):
        return ['admin:*']

    def seed(self, stack, variant):
        self.expected(variant)
        cache = stack.extra.get('p16Seed')
        if cache is not None:
            if cache['variant'] != variant:
                raise ValueError('case already seeded for a different variant')
            return Seeded(copy.deepcopy(cache))
        if stack.extra.get('p16SeedStarted'):
            raise ValueError('incomplete seed; reconcile the original case without retry')
        stack.extra['p16SeedStarted'] = True
        suffix = re.sub('[^a-z0-9]', '', stack.compose_project.lower())[-12:]
        namespace = ('kestrel' if variant == 'A' else 'willow') + '_p16_' + suffix
        town = seed_town(stack, variant, namespace)
        state = {'variant': variant, 'shelters': town['layers']['shelters']}
        stack.extra['p16Seed'] = copy.deepcopy(state)
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        if seeded.state['variant'] != variant:
            raise ValueError('variant differs from original seed')
        stack.extra['recorder'].json('p15-bootstrap-probe', 'GET', '/api/v1/admin/api-keys/', principal='bootstrap')
        # Do not score key metadata, enumerate API keys, consume another actor's
        # secret, or synthesize a before/after credential probe.
        return observe_partial(self.id, stack, seeded, variant)


scenario = P16()
