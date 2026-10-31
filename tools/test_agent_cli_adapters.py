"""Native CLI adapter integrity: W2's real recorder, independent canary, OPS refusal."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

from agent_cli_sessions import CliModelSession
from agent_e2e_cli_runner import target_url
import agent_e2e_local_runner as corpus
import agent_e2e_cli_sessions
from terminal_model_cli_canary import run_canary
from test_agent_cli_sessions import fake_cli
from test_agent_e2e_local_runner import (Candidate, serve, FakeRuntime, FakeP10, FakeModel,
                                        DIGEST, case, CORRECT_WHERE)


def test_fake_cli_corpus_uses_sealed_runner_gateway_and_independent_oracle(tmp_path):
    state=Candidate(); http=serve(state)
    try:
        binary=fake_cli(tmp_path,'claude-cli')
        code=binary.read_text().replace("assert prompt == 'Customer goal only'", 'assert prompt')
        arguments={'serviceId':'p10-shelters','layerId':0,'where':CORRECT_WHERE}
        code=code.replace("{'actual':True}",repr(arguments)).replace('Customer result','Two shelters match: Centre and the North refuge.')
        binary.write_text(code)
        runtime=FakeRuntime(f'http://127.0.0.1:{http.server_port}',FakeModel([]),tmp_path)
        runtime.agent='claude-cli'; runtime.model='claude-sonnet-5-5'
        runtime.cli=CliModelSession(runtime.agent,runtime.model,binary=binary,auth_root=tmp_path)
        receipt=corpus.execute_case(case_id='P10:A:local-docker',scenario=FakeP10(),variant='A',
                                   case=case(),candidate_digest=DIGEST,runtime=runtime)
        assert runtime.torn_down
        assert receipt['status']=='pass', receipt
        assert all(a['status']=='pass' for a in receipt['assertions'])
        assert receipt['sessions'][0]['actions'][0]['tool']=='honua_query_features'
        assert [row[0] for row in state.mcp_calls]==['key-2','admin']  # agent call, then independent controller replay
        assert receipt['sessions'][0]['provenance'][0]['metering']=='subscription'
        assert runtime.fake_model.sent==[]
    finally:
        http.shutdown(); http.server_close()


class JourneyDriver:
    def __init__(self, *, wrong=False, fire=True, approval=True, cleanup=True):
        self.counts={}; self.calls=[]; self.wrong=wrong; self.fire=fire; self.approval=approval; self.cleanup=cleanup
        self.approved=False
    def invoke(self, operation, payload):
        self.calls.append(operation)
        stage=payload.get('stage')
        if operation=='setup': return {'status':'ready','workspaceId':'test','baseUrl':'https://test.cert.demo.honua.io'}
        if operation=='prepare_cli_fixture': return {'status':'pass','attribution':'harness-fixture'}
        if operation=='inject_error': return {'status':'armed','errorId':'cli-render-recovery'}
        if operation=='observe':
            status='complete' if self.counts.get(stage) else 'ready'
            if stage=='8' and not self.approved: status='awaiting_approval'
            return {'status':'pass' if status=='complete' else 'ready',
                    'stageStatus':{'id':stage,'number':int(stage),'command':'test',
                                   'status':'pass' if status=='complete' else status,
                                   'checks':[{'status':'pass'}], 'blockedBy':[]}, 'proposalId':'proposal',
                    'toolView':{'tools':[{'name':'honua_list_capabilities','inputSchema':{'type':'object'}}]}}
        if operation=='execute':
            self.counts[stage]=self.counts.get(stage,0)+1
            result={}
            if stage=='4' and self.fire:
                if self.counts[stage]==1: result['injectedError']={'id':'cli-render-recovery','recoverable':True}
                else: result['recoveredError']={'id':'cli-render-recovery','recovered':True}
            return {'status':'ok','result':result}
        if operation=='approve':
            self.approved=True
            return {'status':'approved','proposalId':'proposal','proposerSelfApproval':'denied' if self.approval else 'allowed',
                    'resolvedBy':'separate-approver','auditId':'audit'}
        if operation=='verify':
            import terminal_model_canary as c
            return {'status':'pass','assertions':dict.fromkeys(c.ASSERTION_NAMES,'pass'),
                    'finalUrlProof':{} if self.wrong else {'proof':'live'},'pixelProof':{'proof':'live'},'canonicalIds':{'id':'live'}}
        if operation=='teardown': return {'status':'pass' if self.cleanup else 'fail'}
        raise AssertionError(operation)


class JourneyCli:
    agent='claude-cli'; model='claude-sonnet-5-5'
    def run(self, **kwargs):
        stage=json.loads(kwargs['prompt'])['stage']['id']
        actions=[]
        for i in range(2 if stage=='4' else 1):
            kwargs['execute']('tool_call',{'kind':'tool_call','tool':'honua_list_capabilities','arguments':{}})
            actions.append({'sequence':i+1,'digest':'sha256:fake','kind':'tool_call'})
        return {'actions':actions,'provenance':[],'finalAnswer':'claims success'}


@pytest.mark.parametrize('failure', [None,'wrong','fire','approval','cleanup'])
def test_cli_canary_retains_independent_verification_and_cleanup(tmp_path, failure):
    driver=JourneyDriver(**({failure:False} if failure in ('fire','approval','cleanup') else {'wrong':failure=='wrong'}))
    journey={'stages':[{'id':str(i),'number':i,'command':'test'} for i in range(1,9)]}
    receipt=run_canary(driver=driver,cli=JourneyCli(),journey=journey,release='test',candidate=DIGEST,
                       setup_request={},workdir=tmp_path)
    assert receipt['status']==('pass' if failure is None else 'infrastructure' if failure=='cleanup' else 'fail')
    assert driver.calls[-1]=='teardown'
    assert 'prepare_cli_fixture' in driver.calls
    if failure is None:
        assert len(receipt['stages'])==8 and len(receipt['approvals'])==1
        assert {e['kind'] for e in receipt['faultEvents']}=={'injectedError','recoveredError'}


@pytest.mark.parametrize('url', ['https://evil.example','http://cell.cert.demo.honua.io',
    'https://cell.cert.demo.honua.io.evil.example','https://key@cell.cert.demo.honua.io',
    'https://cell.cert.demo.honua.io/mcp','https://cell.cert.demo.honua.io?key=x'])
def test_remote_target_rejects_unreviewed_origins(url):
    with pytest.raises(ValueError): target_url(url)


def test_remote_target_accepts_provisioned_cell_origin():
    assert target_url('https://ecs-123.cert.demo.honua.io/')=='https://ecs-123.cert.demo.honua.io'


def test_ops_refuses_before_cli_invocation(monkeypatch):
    path=Path(__file__).resolve().parents[1]/'certification/agent-ops/cli_agent.py'
    spec=importlib.util.spec_from_file_location('test_ops_cli_hook',path)
    module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    def forbidden(*a,**k): raise AssertionError('CLI called for simulated OPS')
    monkeypatch.setattr(module,'CliModelSession',forbidden)
    monkeypatch.setitem(sys.modules,'real_scenarios',None)
    with pytest.raises(RuntimeError,match='not-real'): module.main([])


def test_sequential_runner_stops_on_docker_unhealthiness(monkeypatch):
    import agent_cli_arms as arms
    import subprocess
    monkeypatch.setattr(arms.subprocess,'run',lambda *a,**k: subprocess.CompletedProcess(a,0,b'Up (unhealthy)',b''))
    with pytest.raises(RuntimeError,match='Docker is unhealthy'): arms.docker_healthy()
