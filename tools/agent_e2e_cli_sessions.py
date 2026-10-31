"""Register native CLI implementations of W2's ModelSession protocol.

The recording proxy dispatches through SessionContext.execute. The runner owns
that gateway and seals its own action log; it ignores session-reported actions.
"""
from pathlib import Path
import hashlib
import json
import sys

from agent_cli_sessions import CliFailure, CliModelSession
from agent_e2e_local_api import SessionResult
import agent_e2e_local_runner as runner


class CorpusCliSession:
    def __init__(self, runtime, stack):
        self.runtime = runtime
        self.agent_type = runtime.agent
        self.cli = getattr(runtime, 'cli', None) or CliModelSession(runtime.agent, runtime.model)

    def run(self, context):
        failure = None
        prompt = context.prompt
        if context.attachments:
            attachments = []
            for item in context.attachments:
                raw = Path(item['path']).read_bytes()
                if 'sha256:' + hashlib.sha256(raw).hexdigest() != item['sha256']:
                    raise ValueError('customer attachment changed after staging')
                attachments.append({'name':item['name'], 'mediaType':item['mediaType'],
                                    'sha256':item['sha256'], 'content':raw.decode('utf-8')})
            prompt = json.dumps({'customerRequest':context.prompt, 'customerAttachments':attachments},
                                ensure_ascii=False, separators=(',', ':'))
        try:
            row = self.cli.run(prompt=prompt, tool_view=context.tool_view,
                execute=context.execute, log_dir=Path(context.workdir)/'cli-wire', session=context.session,
                max_actions=context.limits.max_actions, max_turns=context.limits.max_model_calls,
                timeout=getattr(self.runtime, 'timeout', 600))
        except CliFailure as exc:
            row = exc.progress
            failure = {'category': exc.category, 'reason': str(exc)}
        if row['tokens'] + context.token_budget_used > context.limits.max_tokens:
            failure = {'category': 'model', 'reason': 'token bound exceeded'}
        if hasattr(self.runtime, 'cli_records'): self.runtime.cli_records.extend(row['provenance'])
        return SessionResult(agent_type=self.agent_type, model_id=self.runtime.model,
            final_answer=row.get('finalAnswer'), temperature=None, temperature_policy='CLI default',
            tokens=row['tokens'], usage=tuple(row['usage']), cost_usd=None,
            model_visible_digests=tuple(row['modelVisibleDigests']), provenance=tuple(row['provenance']),
            message_ids=tuple(row['messageIds']), failure=failure,
            extra={'systemContractSha256': row['systemContractSha256'], 'toolViewSha256': row['toolViewSha256']})


_main = sys.modules.get('__main__')
_runners = [runner]
if _main is not runner and getattr(_main, '__file__', None) == runner.__file__:
    _runners.append(_main)
for _registry in _runners:
    for _name in ('claude-cli', 'codex-cli'):
        if _name not in _registry.SESSIONS: _registry.register_session(_name, CorpusCliSession)
