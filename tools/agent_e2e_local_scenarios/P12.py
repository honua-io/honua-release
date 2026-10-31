"""P12 is unsupported: a fresh-session read after restart cannot yet be proven.

The runner can restart between sessions, but the restart fact is not in the
resourceId/states shape required by the lifecycle oracle, and the projection
language cannot extract a stopped state from it. The scorer also rejects
between-phase reads outside protected-content baselines. A second-session map
read must be bound to that session and the seeded version.
StudioSetup.prepare_saved_map additionally requires a real SQL absence baseline
and separate task and setup transports, which the runner does not expose.
"""
from agent_e2e_local_api import Scenario


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
        raise RuntimeError('P12 unsupported: lifecycle and fresh-session read cannot yet be scored')


scenario = P12()
