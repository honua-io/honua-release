"""P12 is unsupported: a fresh-session read after restart cannot yet be proven.

The runner records an oracle-compatible restart and exposes read-only SQL.
A reviewed saved-town-map envelope and a recorder-compatible fixture adapter
are still needed to bind the seeded map/version and source baseline; inventing
a map fixture would not establish the requested starting state.
"""
from agent_e2e_local_api import Observation, Scenario


class P12(Scenario):
    id = 'P12'
    supported_variants = ()

    def sessions_required(self):
        return 2

    def between_sessions(self, stack, seeded, variant, after_session):
        if after_session != 1 or variant not in ('A', 'B'):
            raise ValueError('invalid between-session request')
        return {'restartServer': True}

    def seed(self, stack, variant):
        raise RuntimeError('P12 unsupported: no independent saved-map setup baseline')

    def observe(self, stack, seeded, variant, run=None):
        return [Observation('restart.observed', None, {'projection': 'extract/v1'})]


scenario = P12()
