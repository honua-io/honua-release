"""P12 is unsupported: a fresh-session read after restart cannot yet be proven.

The runner can now record an oracle-compatible restart between sessions. A
second-session map read still needs to be bound to the seeded version and
fixture-derived expected content and hash.
StudioSetup.prepare_saved_map additionally requires a real SQL absence baseline
and separate task and setup transports, which the runner does not expose.
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
