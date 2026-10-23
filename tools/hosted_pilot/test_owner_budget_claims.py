"""New source versions cannot reopen an original publication allowance."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import deploy


class OwnerBudgetClaims(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        policy = self.root/'certification/agent-e2e/foss4g-pilot.v1.json'
        policy.parent.mkdir(parents=True)
        policy.write_text(json.dumps({'policy': {'totalBudgetUsd':2000,'cleanupReserveUsd':200,'maxConcurrentPaidAttempts':4}}))
        self.config = self.root/'image-preparation.tfvars.json'
        self.config.write_text('{"region":"us-east-1"}\n')
        self.ledger = {'schema':'honua.hosted-pilot-budget/v1','expires_at':'2099-01-01T00:00:00Z',
            'total_budget_usd':2000,'cleanup_reserve_usd':200,'spent_usd':0,'reserved_usd':294,
            'deployment_reservation_usd':5,'active_paid_attempts':0,'source_sha':'a'*40,
            'config_sha256':deploy.digest(self.config),'reservation_id':'original-image-allocation-001'}
        self.objects = {}
        self.env = {'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'42','PILOT_CONFIG_PATH':'reviewed-source',
            'RUNNER_TEMP':self.temp.name,'PILOT_PLAN_BUCKET':'original-plans','PILOT_PLAN_KMS_ARN':'original-key',
            'PILOT_BUDGET_VERSION_ID':'original-version'}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,self.env,clear=True).start()
        patch.object(deploy,'ROOT',self.root).start()
        patch.object(deploy,'config',return_value=self.config).start()
        patch.object(deploy,'aws',side_effect=self.aws).start()
        patch.object(deploy.subprocess,'run',side_effect=self.put).start()

    def aws(self,*args):
        self.assertEqual(args[:2],('s3api','get-object'))
        key = args[args.index('--key')+1]
        value = self.ledger if key == 'hosted-pilot/budget/ledger.json' else self.objects[key]
        Path(args[-1]).write_text(json.dumps(value))
        return {}

    def put(self,args,**kwargs):
        self.assertEqual(args[:3],['aws','s3api','put-object'])
        self.assertEqual(args[args.index('--if-none-match')+1],'*')
        key = args[args.index('--key')+1]
        code = 1 if key in self.objects else 0
        if not code:self.objects[key]=json.loads(Path(args[args.index('--body')+1]).read_text())
        return subprocess.CompletedProcess(args,code,stdout=b'',stderr=b'')

    def admit(self):
        raw=json.dumps(self.ledger).encode()
        os.environ['PILOT_BUDGET_SHA256']=hashlib.sha256(raw).hexdigest()
        return deploy.budget()

    def test_same_original_run_reconciles_ack_loss_without_second_claim(self):
        first=self.admit();snapshot=copy.deepcopy(self.objects)
        self.assertEqual(self.admit(),first)
        self.assertEqual(self.objects,snapshot)
        self.assertEqual(list(self.objects),['hosted-pilot/reservations/original-image-allocation-001.json'])

    def test_changed_source_or_ledger_version_cannot_reopen_original_allocation(self):
        self.admit();snapshot=copy.deepcopy(self.objects)
        self.ledger['source_sha']='b'*40
        os.environ.update(GITHUB_SHA='b'*40,GITHUB_RUN_ID='43',PILOT_BUDGET_VERSION_ID='new-version')
        with self.assertRaisesRegex(ValueError,'already claimed by another run'):
            self.admit()
        self.assertEqual(self.objects,snapshot)

    def test_invalid_allocation_identity_cannot_write_outside_claim_namespace(self):
        for identity in ('../outside','foreign/path','x'):
            self.ledger['reservation_id']=identity
            with self.subTest(identity=identity),self.assertRaisesRegex(ValueError,'reservation identity is invalid'):
                self.admit()
        self.assertEqual(self.objects,{})
