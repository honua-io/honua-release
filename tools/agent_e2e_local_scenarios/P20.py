"""The runner needs a verified pair of same-named saved maps, authenticated operator and anonymous controls, a model-visible stable-id handoff, and a controller-verified publication event ordered after both clarification answers. The runner assigns the next topic after any question rather than verifying what was asked; it must record topic-matched clarification events, bind reply IDs as logical roles and prove publication followed both answers.

The case stays unsupported until every pack assertion can be independently scored.
"""
from __future__ import annotations

REASON = 'The runner needs a verified pair of same-named saved maps, authenticated operator and anonymous controls, a model-visible stable-id handoff, and a controller-verified publication event ordered after both clarification answers. The runner assigns the next topic after any question rather than verifying what was asked; it must record topic-matched clarification events, bind reply IDs as logical roles and prove publication followed both answers.'

from agent_e2e_local_api import Scenario as BaseScenario
from agent_e2e_local_projections_w2g4 import observe_partial, role_bindings


class LocalScenario(BaseScenario):
    id = "P20"
    supported_variants = ()

    def seed(self, stack, variant):
        self.expected(variant)  # validate the development variant before touching the stack
        raise NotImplementedError("P20: " + REASON)

    def needs_approver(self):
        return False

    def roles(self, stack, seeded, variant, run=None):
        return role_bindings(self.id, seeded)

    def observe(self, stack, seeded, variant, run=None):
        return observe_partial(self.id, stack, seeded, variant)

    def cleanup(self, stack, seeded):
        if seeded.state:
            raise ValueError("Unsupported scenario cannot claim cleanup ownership")


scenario = LocalScenario()
