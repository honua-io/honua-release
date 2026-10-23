"""Actual private-child protocol and original financial fence controls."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock
import browser_render as render
from agent_e2e_native_costs import OVERHEAD, reserve_overhead
from agent_e2e_pack import canonical_digest, PackError
from agent_e2e_state import DurableExecution, preserve_history

CANDIDATE = 'sha256:'+'a'*64
CONTEXT = {'componentBindingDigest': CANDIDATE, 'campaignId': 'offline-browser', 'caseId': 'P14:A:ecs-fargate', 'attempt': 1, 'campaignMode': 'development'}

class Store:
    candidate = CANDIDATE
    state = None
    def acquire(self): return copy.deepcopy(self.state)
    def commit(self, value):
        preserve_history(self.state, value); self.state = copy.deepcopy(value)
    def renew(self): pass
    def release(self): pass

class PrivateBrowser(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = {'playwright': '1.61.1', 'chromium': '149.0.0.0'}
        self.state = {'cookies': [{'value': 'private-sentinel-cookie'}], 'origins': []}
        self.calls = 0
    def authorize(self): self.calls += 1
    def child(self, body):
        path = self.root/'child.py'
        path.write_text('import json,os,sys,time\n'+body)
        return [sys.executable, str(path)]
    def run_child(self, body, *, authorize=None, timeout=2):
        return render.run_browser(contract={'number': 1.0, 'unicode': 'Mānoa'}, runtime=self.runtime,
            state=self.state, authorize_effect=authorize or self.authorize, timeout=timeout,
            command=self.child(body), work_directory=self.root)
    def test_actual_stdin_float_unicode_handshake_and_private_environment(self):
        code = '''request=json.loads(sys.stdin.readline())
assert request['contractJson']=='{"number":1.0,"unicode":"Mānoa"}'
assert request['storageState']['cookies'][0]['value']=='private-sentinel-cookie'
assert not any(key.startswith(('AWS_', 'PILOT_', 'HONUA_AGENT_')) for key in os.environ)
for i in (1,2):
 print(json.dumps({'type':'authorize','id':i}),flush=True)
 assert json.loads(sys.stdin.readline())=={'type':'authorized','id':i}
print(json.dumps({'type':'report','report':{'browserRuntime':request['runtime'],'safe':'actual-protocol'}}),flush=True)
print('{"type":"complete"}',flush=True)
'''
        result = self.run_child(code)
        self.assertEqual(result['safe'], 'actual-protocol'); self.assertEqual(self.calls, 4)
        self.assertEqual([p.name for p in self.root.iterdir()], ['child.py'])
    def test_fence_loss_terminates_child_and_cleans_private_directory(self):
        def fence():
            self.calls += 1
            if self.calls == 2: raise PackError('lease lost')
        code = '''json.loads(sys.stdin.readline())
print('{"type":"authorize","id":1}',flush=True)
time.sleep(10)
'''
        with self.assertRaisesRegex(ValueError, 'preserve original attempt'): self.run_child(code, authorize=fence)
        self.assertEqual([p.name for p in self.root.iterdir()], ['child.py'])
    def test_dependency_paths_preserve_private_home_and_reject_credentials(self):
        code = '''request=json.loads(sys.stdin.readline())
assert os.environ['HOME']==os.environ['TMPDIR']
assert os.path.isdir(os.environ['HOME'])
assert os.environ['PLAYWRIGHT_BROWSERS_PATH']=='/opt/honua-render/browsers'
assert os.environ['HONUA_RENDER_CHROMIUM']=='/exact/canonical/chromium'
print('{"type":"authorize","id":1}',flush=True)
assert json.loads(sys.stdin.readline())=={'type':'authorized','id':1}
print(json.dumps({'type':'report','report':{'browserRuntime':request['runtime']}}),flush=True)
print('{"type":"complete"}',flush=True)
'''
        render.run_browser(contract={}, runtime=self.runtime, state=self.state,
            authorize_effect=self.authorize, timeout=2, command=self.child(code), work_directory=self.root,
            environment={'HONUA_RENDER_CHROMIUM': '/exact/canonical/chromium'})
        self.assertEqual([p.name for p in self.root.iterdir()], ['child.py'])
        for override in ({'AWS_ACCESS_KEY_ID': 'private-sentinel'}, {'HOME': '/ambient/home'}):
            with self.subTest(override=next(iter(override))), self.assertRaisesRegex(ValueError, 'unreviewed environment authority'):
                render.run_browser(contract={}, runtime=self.runtime, state=self.state,
                    authorize_effect=self.authorize, timeout=2, command=self.child(code), work_directory=self.root,
                    environment=override)
        self.assertEqual([p.name for p in self.root.iterdir()], ['child.py'])
    def test_nonreading_child_is_bounded_by_deadline(self):
        self.state = {'cookies': [{'value': 'a'*200000}], 'origins': []}
        with self.assertRaisesRegex(ValueError, 'private browser observation failed'):
            self.run_child('time.sleep(10)\n', timeout=.1)
        self.assertEqual([p.name for p in self.root.iterdir()], ['child.py'])
    def test_unknown_failure_duplicate_sequence_and_missing_complete_cannot_export_report(self):
        for event in ('{"type":"failed","code":"private-sentinel-cookie"}', '{"type":"authorize","id":2}', '{"type":"complete"}'):
            with self.subTest(event=event), self.assertRaisesRegex(ValueError, '^private browser observation failed; preserve original attempt and liabilities$'):
                self.run_child('sys.stdin.readline()\nprint('+repr(event)+',flush=True)\n')

class BrowserFinancialFence(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.durable = DurableExecution(Store(), Path(self.temp.name)/'ledger.json', initialize=True)
        self.addCleanup(self.durable.close)
        for kind in ('infrastructure', 'cleanup'):
            self.durable.update(self.durable.path, 'reserve', candidate_key=CANDIDATE, reservation_id=kind,
                campaign_id=CONTEXT['campaignId'], case_id=CONTEXT['caseId'], attempt=1, campaign_mode='development',
                kind=kind, amount='1', basis_digest='sha256:'+'b'*64)
        self.durable.update(self.durable.path, 'reserve-operation', candidate_key=CANDIDATE,
            reservation_id='original-overhead', operation_id='original-controller', purpose='shared-environment',
            kind='infrastructure', amount='1', basis_digest='sha256:'+'c'*64)
        self.coverage = {'schemaVersion': 1, 'allocation': 'compute-infrastructure-including-cleanup', 'bindings': [{
            'reservationId': 'original-overhead', 'basisDigest': 'sha256:'+'c'*64, 'categories': sorted(OVERHEAD),
            'perAttemptCeilingUsd': '0.25', 'maximumAttempts': 4}]}
        reserve_overhead(self.durable, CONTEXT, self.coverage)
    def check(self): return render.authorize(self.durable, CONTEXT, canonical_digest(self.coverage))
    def test_original_nonzero_allowances_revalidated_without_new_reservation_or_charge(self):
        prior = copy.deepcopy(self.durable.state)
        self.assertEqual(self.check(), canonical_digest({key:CONTEXT[key] for key in ('campaignId','caseId','attempt')}))
        self.assertEqual(self.durable.state, prior)
    def test_missing_overhead_settled_operation_and_zero_capacity_refuse(self):
        original = copy.deepcopy(self.durable.state)
        attempt = next(iter(original['nativeOverheadAllocations']))
        for change in ('missing', 'settled', 'zero', 'missing-cleanup'):
            self.durable.state = copy.deepcopy(original)
            if change == 'missing': self.durable.state['nativeOverheadAllocations'] = {}
            elif change == 'settled': self.durable.state['ledger']['operations'][0].update(state='settled', actualUsd='1')
            elif change == 'zero': self.durable.state['nativeOverheadAllocations'][attempt]['bindings'][0]['perAttemptCeilingUsd'] = '0'
            else: self.durable.state['ledger']['entries'][1].update(state='settled', actualUsd='1')
            with self.subTest(change=change), self.assertRaises((ValueError, PackError)): self.check()
