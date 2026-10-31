"""P08 compose a map.

The runner reads the saved map through the controller API and renders it in its own
browser. Pack prompts stay in the pack. This module names the reads.
"""
from __future__ import annotations

from agent_browser_studio import (
    composition_spec, handoff_spec, observe_map, render_spec, seed_case, source_content_spec,
)
from agent_e2e_local_api import Observation, RoleBinding, Scenario


class P08(Scenario):
    id = 'P08'
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        return seed_case(self.id, stack, variant, create_map=False)

    def roles(self, stack, seeded, variant, run=None):
        return [RoleBinding('created:map', 'p08-maps', items='/data/items', id='/itemId',
                            owner='/ownerId', before='p08-maps-before')]

    def observe(self, stack, seeded, variant, run=None):
        observe_map(stack, seeded, 'p08', created=True)
        return [
            Observation('map.composition', None, {'projection': 'extract/v1', 'spec': composition_spec('p08', 'created:map')}),
            Observation('render.actual', None, {'projection': 'extract/v1', 'spec': render_spec('p08')}),
            Observation('source-content.unchanged', None, {'projection': 'extract/v1', 'spec': source_content_spec('p08')}),
        ]

    def handoff(self, stack, seeded, variant, run=None):
        return handoff_spec('p08', 'created:map')


scenario = P08()
