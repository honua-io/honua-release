"""Model-selected terminal actions on the same independently observed worker."""
import copy

from agent_e2e_mcp_backend import verify_dispatch
from agent_e2e_pack import PackError, canonical_digest


class TerminalActions:
    def __init__(self, *, durable, lifecycle, launch, worker, retain):
        self.durable, self.lifecycle, self.worker = durable, lifecycle, worker
        self.launch, self.retain = copy.deepcopy(launch), retain

    def execute(self, context, action, dispatch, *, timeout=300):
        def authorize():
            self.durable.require_worker_launch(self.launch, launching=True)
            verify_dispatch(self.durable.state, self.durable.store.candidate, self.launch,
                            context, action, dispatch, names=('terminal',))
            if canonical_digest(self.worker.config) != self.lifecycle.config['transportDigest']:
                raise PackError('terminal transport differs from reviewed launch')
        authorize()
        observed = self.lifecycle.observe(self.launch)
        if observed['executionStatus'] != 'RUNNING' or observed['taskStatus'] != 'RUNNING' or observed['stopRecorded']:
            raise PackError('terminal worker is not independently observed running')
        intent = {'context': copy.deepcopy(context), 'action': copy.deepcopy(action), 'dispatch': copy.deepcopy(dispatch)}
        self.retain('terminal-intent-'+dispatch['actionId'], intent)
        authorize()
        reply = self.worker.execute(action['input']['command'], dispatch['actionId'],
            timeout=min(300, int(timeout), action['input'].get('timeoutSeconds', 300)))
        reference = self.retain('terminal-reply-'+dispatch['actionId'], reply)
        self.durable.retain_worker_action({'schemaVersion': 1, 'candidateKey': self.launch['candidateKey'],
            'attemptId': self.launch['attemptId'], 'actionId': dispatch['actionId'],
            'launchDigest': canonical_digest(self.launch), 'context': copy.deepcopy(self.launch['context']),
            'ordinal': dispatch['ordinal'], 'requestDigest': dispatch['requestDigest'],
            'intentDigest': dispatch['intentDigest'], 'replyDigest': canonical_digest(reply), 'reply': reference}, self.launch)
        if reply['status'] != 'completed':
            raise PackError('terminal action interrupted; retain evidence without redispatch')
        failed = reply['exitCode'] != 0
        return {'agentResult': {'content': {key: reply[key] for key in ('exitCode', 'stdout', 'stderr')},
            'isError': failed, 'errorCategory': 'other' if failed else None}, 'operationCategory': 'other', 'telemetry': []}
