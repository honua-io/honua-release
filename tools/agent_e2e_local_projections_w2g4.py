"""Unavailable governance assertions fail closed; never provide synthetic evidence.

The current runner cannot freeze the dynamic identities needed by P14/P16/P17/
P20. Their modules retain this inventory while support is blocked. P15 implements
its own recorder observations. P18 is excluded by the frozen preregistration.
"""
from agent_e2e_local_api import Observation
from agent_e2e_local_expectations import load_expected, load_roles


def role_bindings(scenario_id, seeded):
    return load_roles(scenario_id)


def observe_partial(scenario_id, stack, seeded, variant):
    return [Observation(assertion, None, {'projection': 'extract/v1'})
            for assertion in load_expected(scenario_id, variant)]
