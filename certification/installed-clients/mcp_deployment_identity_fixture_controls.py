"""Offline failure controls for the owned two-client cohort coordinator.

Injected transports are not candidate, role API, Redis or tenant evidence.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import mcp_deployment_identity_fixture as subject

RUNTIME={"containerId":"c"*64,"imageId":"sha256:"+"d"*64,"candidateSourceSha":"a"*40}

class CoordinatorFailureControls(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root=Path(self.directory.name);self.root.chmod(0o700)
        self.args=SimpleNamespace(private_root=str(self.root),state_path=str(self.root/'cohort.json'),
            nonce='e'*12,service_id='owned_area',credential_salt='f'*32,
            candidate_source_sha='a'*40,candidate_image='repo@sha256:'+'d'*64,
            sdk_journey=None,inspector_journey=None)
        self.inspect=lambda:copy.deepcopy(RUNTIME)

    def state(self):
        return {"schema":"honua.mcp-deployment-managed-cohort/v1","nonce":self.args.nonce,
            "serviceId":self.args.service_id,"credentialSalt":self.args.credential_salt,
            "candidateImage":self.args.candidate_image,"candidate":subject.candidate(RUNTIME),
            "configuration":subject.declaration('mcp-deployment-'+self.args.nonce),
            "configurationObservation":{},"roleReceipt":{"roleId":"owned-role"},
            "keys":{"sdk":{},"inspector":{}},"failures":[],"cleanupComplete":False}

    def test_invalid_service_refuses_before_shared_role_or_candidate_access(self):
        self.args.service_id='*/not-owned'
        with patch.object(subject,'normal_role') as role,patch.object(subject,'observe_configuration') as observe:
            with self.assertRaisesRegex(ValueError,'exact workspace execution service required'):
                subject.prepare(self.args,self.inspect)
            role.assert_not_called();observe.assert_not_called()
        self.assertFalse(Path(self.args.state_path).exists())

    def test_partial_key_creation_cleans_every_attempt_and_shared_role_and_retains_failure(self):
        calls=[]
        class Role:
            role_id='owned-role'
            receipt={"roleId":"owned-role","cleanupComplete":False}
            def create(self):calls.append('role-create')
            def revoke(self):calls.append('role-revoke');self.receipt["cleanupComplete"]=True
        class Identity:
            count=0
            def __init__(self,**kwargs):
                self.number=Identity.count;Identity.count+=1
                self.receipt={"cleanupComplete":False}
            def create(self):
                calls.append('create-'+str(self.number))
                if self.number==2:raise ValueError('injected partial provisioning refusal')
            def revoke(self):
                calls.append('revoke-'+str(self.number));self.receipt["cleanupComplete"]=True
        with patch.object(subject,'normal_role',return_value=Role()),patch.object(subject,'ManagedIdentityFixture',Identity),patch.object(subject,'observe_configuration',return_value={}):
            with self.assertRaisesRegex(ValueError,'injected partial provisioning refusal'):
                subject.prepare(self.args,self.inspect)
        self.assertEqual(calls,['role-create','create-0','create-1','create-2','revoke-2','revoke-1','revoke-0','role-revoke'])
        retained=subject.read_state(Path(self.args.state_path))
        self.assertEqual(retained['failures'],[{"phase":"prepare"}]);self.assertFalse(retained['cleanupComplete'])

    def test_role_constructor_failure_does_not_skip_four_owned_key_cleanups(self):
        subject.persist(Path(self.args.state_path),self.state(),initial=True)
        calls=[]
        def cleanup(command):
            calls.append(command)
            return 0
        with patch.object(subject,'managed_main',side_effect=cleanup),patch.object(subject,'normal_role',side_effect=ValueError('injected role observation refusal')):
            result=subject.close(self.args,self.inspect)
        self.assertEqual(len(calls),4)
        self.assertEqual(len({call[call.index('--private-path')+1] for call in calls}),4)
        self.assertFalse(result['cleanupComplete'])
        self.assertIn({"phase":"cohort-cleanup"},result['failures'])
        self.assertEqual(subject.read_state(Path(self.args.state_path)),result)

    def test_one_key_cleanup_failure_does_not_skip_other_keys_or_owned_role_cleanup(self):
        subject.persist(Path(self.args.state_path),self.state(),initial=True)
        calls=[]
        class Role:
            role_id=None;grants=[];receipt={}
            def bind_journeys(self,originals):
                raise ValueError('both actual original journeys absent')
            def revoke(self):
                calls.append('role-revoke');self.receipt['cleanupComplete']=True
        def cleanup(command):
            calls.append(command)
            if len(calls)==1:raise ValueError('injected owned transport failure')
            return 0
        with patch.object(subject,'managed_main',side_effect=cleanup),patch.object(subject,'normal_role',return_value=Role()):
            result=subject.close(self.args,self.inspect)
        self.assertEqual(len(calls),5);self.assertEqual(calls[-1],'role-revoke')
        self.assertIn({"phase":"key-cleanup","client":"sdk","role":"primary"},result['failures'])
        self.assertIn({"phase":"shared-journey-binding"},result['failures'])
        self.assertFalse(result['cleanupComplete'])
        self.assertEqual(subject.read_state(Path(self.args.state_path)),result)

    def test_receipt_write_failure_does_not_strand_remaining_owned_keys(self):
        subject.persist(Path(self.args.state_path),self.state(),initial=True)
        cleanup=[];writes=[];original_persist=subject.persist
        def persist(*args,**kwargs):
            writes.append(1)
            if len(writes)==1:raise OSError('injected public receipt write refusal')
            return original_persist(*args,**kwargs)
        with patch.object(subject,'persist',side_effect=persist),patch.object(subject,'managed_main',side_effect=lambda command:cleanup.append(command) or 0),patch.object(subject,'normal_role',side_effect=ValueError('injected role observation refusal')):
            result=subject.close(self.args,self.inspect)
        self.assertEqual(len(cleanup),4)
        self.assertIn({"phase":"key-receipt-persistence","client":"sdk","role":"primary"},result['failures'])
        self.assertFalse(result['cleanupComplete'])
        self.assertEqual(subject.read_state(Path(self.args.state_path)),result)

if __name__=='__main__':unittest.main()
