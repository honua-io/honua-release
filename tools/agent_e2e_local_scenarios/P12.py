"""P12 is unsupported: a fresh-session read after restart cannot yet be proven.

The runner executes both sessions before observe(), so its restartServer helper
cannot put a restart between them. It must expose a between-session restart hook
and bind the second session's map read to that session and the seeded version.
StudioSetup.prepare_saved_map additionally requires a real SQL absence baseline
and separate task and setup transports, which the runner does not expose.
"""
from agent_e2e_local_api import Scenario


class P12(Scenario):
    id = 'P12'
    supported_variants = ()

    def seed(self, stack, variant):
        raise RuntimeError('P12 unsupported: no independent saved-map setup baseline')

    def observe(self, stack, seeded, variant, run=None):
        raise RuntimeError('P12 unsupported: no restart between the model sessions')


scenario = P12()
