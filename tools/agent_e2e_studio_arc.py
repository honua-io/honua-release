"""AI arc stage 4 (maps with Studio, in Honua Console): both required drivers, fail-closed.

  console-assistant   Console's built-in AI assistant (Studio AI proxy, Bedrock Sonnet 4.6) driven by a
                      scripted customer signed in to Console as the customer principal. Driver module:
                      ``agent_e2e_console_assistant`` (exports ``run_assistant(ctx, scenarios, script)``).
  arm-browser         the CLI arm in the Studio UI through #1162's origin-locked Console browser: the
                      #1152 corpus case for each Studio scenario, run on the arc's own stack with the
                      ``studio`` MCP server #1162 adds (``agent_browser_studio``).

Both are scored from saved map/style documents (controller reads) and an independent render through
the server's map rendering API (the scenarios' frozen projections); never DOM markers. Until each
driver's module is present, the stage is unavailable naming what is missing.
"""
from __future__ import annotations


def arc_stage(ctx):
    return StudioArc(ctx)


class StudioArc:
    stage, lane = 'studio', '#1162 Console browser + Console assistant driver'

    def __init__(self, ctx):
        stages = {s['id']: s for s in ctx.arc['stages']}
        self.scenarios = tuple(stages['studio']['scenarios'])
        self.script = ctx.arc['studioAssistant']['customerScript']

    def unavailable(self, ctx):
        try:
            import agent_browser_studio  # noqa: F401
        except ImportError:
            return "the origin-locked Console browser for CLI arms (#1162 agent_browser_studio) has not landed"
        try:
            import agent_e2e_console_assistant  # noqa: F401
        except ImportError:
            return ("the Console built-in assistant driver with the scripted customer "
                    "(agent_e2e_console_assistant) has not landed")
        try:
            import agent_e2e_local_runner as runner
        except ImportError:
            return 'the corpus runner is not installed'
        missing = []
        for sid in self.scenarios:
            try:
                if ctx.variant not in runner.load_scenario(sid).supported_variants:
                    missing.append(sid)
            except Exception:  # noqa: BLE001
                missing.append(sid)
        if missing:
            return 'Studio scenarios not yet observable for this variant: ' + ','.join(missing)
        return None

    def sign_in_probe(self, ctx):
        """The customer principal signs in to this Console through #1162's trusted-edge sign-in."""
        import agent_browser_studio
        probe = getattr(agent_browser_studio, 'customer_sign_in_probe', None)
        if probe is None:
            return {'ok': False, 'reason': 'agent_browser_studio exports no customer_sign_in_probe'}
        return probe(ctx.slot.facts['consoleBaseUrl'], ctx.slot.controller)

    def prepare(self, ctx):
        return None

    def notes(self, ctx):
        return None

    def attachment(self, ctx):
        return None

    def run(self, ctx, session, prompt):
        import ai_arc_services
        import agent_e2e_console_assistant as assistant
        template, previous = ctx.handoff
        handoff = template.replace('{previous}', previous.strip()[:4000]) if template and previous else None
        browser_row, browser_cases = ai_arc_services.run_services(
            scenarios=self.scenarios, variant=ctx.variant, candidate_digest=ctx.candidate['imageDigest'],
            base_url=ctx.slot.controller['base_url'], admin_key=ctx.slot.controller['admin_key'],
            project=ctx.slot.project + '-studio', arm=ctx.arm, limits=ctx.limits,
            out_dir=ctx.private_dir / 'studio' / 'browser', handoff=handoff, extra_secrets=ctx.slot.harness_secrets,
            deadline=ctx.deadline, private_root=ctx.private_dir / 'studio' / 'cases', register=ctx.live.append,
            gate=ctx.gate)
        assistant_result = assistant.run_assistant(ctx, self.scenarios, self.script)
        self.cases = {'arm-browser': browser_cases, 'console-assistant': assistant_result['cases']}
        return {'actions': browser_row['actions'] + assistant_result.get('actions', []),
                'finalAnswer': (browser_row['answers'] or [None])[-1], 'utterances': browser_row['answers'],
                'nudges': browser_row['nudges'], 'failure': browser_row['failure'] or assistant_result.get('failure'),
                'nestedAnswerChecks': True, 'assistantModel': assistant_result.get('observedModel')}

    def observe(self, ctx, row):
        import ai_arc_services
        rows = []
        for driver, cases in self.cases.items():
            rows.extend({**r, 'assertion': f"{driver}:{r['assertion']}"} for r in ai_arc_services.case_assertions(cases))
        return rows

    def handoff_observations(self, ctx, answer):
        return None

    def deployment_hosts(self, ctx):
        return ('localhost', '127.0.0.1', '::1')

    def approvals(self, ctx):
        return []

    def scope(self, ctx):
        return None
