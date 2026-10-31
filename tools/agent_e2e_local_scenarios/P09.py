"""P09 style and labels.

The runner seeds one unstyled saved map, then reads the saved document and renders it
in its own browser. Pack prompts stay in the pack.
"""
from __future__ import annotations

from agent_browser_studio import (
    handoff_spec, observe_map, render_spec, saved_spec, seed_case, source_content_spec, style_spec,
)
from agent_e2e_local_api import Observation, RoleBinding, Scenario


class P09(Scenario):
    id = 'P09'
    supported_variants = ('A', 'B')

    def seed(self, stack, variant):
        return seed_case(self.id, stack, variant, create_map=True)

    def roles(self, stack, seeded, variant, run=None):
        return [
            RoleBinding('seeded:map', 'p09-maps-before', pointer='/data/items/0/itemId'),
            RoleBinding('created:version', 'p09-maps', items='/data/items', id='/currentVersionId',
                        owner='/ownerId', before='p09-maps-before'),
        ]

    def observe(self, stack, seeded, variant, run=None):
        observe_map(stack, seeded, 'p09', created=False)
        return [
            Observation('map.style', None, {'projection': 'extract/v1', 'spec': style_spec('p09', 'seeded:map')}),
            Observation('render.actual', None, {'projection': 'extract/v1', 'spec': render_spec('p09')}),
            Observation('state.saved', None, {'projection': 'extract/v1', 'spec': saved_spec('p09', 'seeded:map', 'created:version')}),
            Observation('source-content.unchanged', None, {'projection': 'extract/v1', 'spec': source_content_spec('p09')}),
        ]

    def handoff(self, stack, seeded, variant, run=None):
        return handoff_spec('p09', 'seeded:map')


scenario = P09()
