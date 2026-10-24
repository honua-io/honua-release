"""Synthetic registrar admission/quote controls; no real ledger or cloud writes."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import registration_allowance as admission
from agent_e2e_pack import canonical_digest


class RegistrationAdmission(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.now=2000000000
        self.candidate='sha256:'+'a'*64;self.generation='sha256:'+'b'*64
        self.image='123456789012.dkr.ecr.us-east-1.amazonaws.com/honua-pilot-test-trusted-controller@sha256:'+'c'*64
        self.config={'candidateKey':self.candidate,'region':'us-east-1','accountId':'123456789012',
            'state':{'region':'us-east-1','bucket':'original-accounting','table':'original-head'},
            'generationDigest':self.generation,'bootstrapArtifactDigest':'sha256:'+'d'*64,
            'declaration':{'path':'certification/agent-e2e/installed-runtimes/runtime.json','sha256':'sha256:'+'e'*64},
            'imageUri':self.image,'maxExecutionSeconds':600,'maximumUsd':{'infrastructure':'1.00','cleanup':'1.00'}}
        self.operations=[{'id':'shared-'+kind,'operationId':'original-network','purpose':'shared-environment','kind':kind,
            'amountUsd':'10.00','basisDigest':'sha256:'+'f'*64,'state':'reserved','actualUsd':None} for kind in ('infrastructure','cleanup')]
        self.durable=MagicMock();self.durable.store.candidate=self.candidate;self.durable.store.generation={'digest':self.generation}
        self.durable.store.bucket='original-accounting';self.durable.store.table='original-head'
        self.durable.store.owner='actual-owner';self.durable.store.fence=8;self.durable.store.version=7
        self.durable.state={'ledger':{'candidateKey':self.candidate,'operations':self.operations,
            'interactiveAccounting':{'originalExpiresEpoch':self.now+3600}},
            'bootstrapImport':{'artifactDigest':self.config['bootstrapArtifactDigest']}}
        self.durable.store.db.get_item.return_value={'Item':{'leaseOwner':{'S':'actual-owner'},'fence':{'N':'8'},
            'leaseUntil':{'N':str(self.now+120)},'stateVersion':{'N':'7'},'generationDigest':{'S':self.generation},
            'snapshotDigest':{'S':'sha256:'+'1'*64},'snapshotVersion':{'S':'original-snapshot-version'}}}
        self.persisted=[]
        self.durable._persist.side_effect=lambda:self.persisted.append(copy.deepcopy(self.durable.state))
        def reserve(path,operation,**kw):
            self.assertEqual(operation,'reserve-operation')
            self.assertTrue(self.durable.state.get('runtimeRegistrationAdmissions'))
            self.operations.append({'id':kw['reservation_id'],'operationId':kw['operation_id'],'purpose':kw['purpose'],
                'kind':kw['kind'],'amountUsd':kw['amount'],'basisDigest':kw['basis_digest'],'state':'reserved','actualUsd':None})
            self.persisted.append(copy.deepcopy(self.durable.state))
        self.durable.update.side_effect=reserve
        prices=[]
        for kind,usage,unit,price in [('fargate-vcpu','USE1-Fargate-vCPU-Hours:perCPU','hours','0.0404800000'),
            ('fargate-memory','USE1-Fargate-GB-Hours','hours','0.0044450000'),
            ('nat-transfer','NatGateway-Bytes','GB','0.0450000000'),
            ('cloudwatch-ingestion','USE1-DataProcessing-Bytes','GB','0.5000000000')]:
            original={'PriceList':[json.dumps({'product':{'attributes':{'regionCode':'us-east-1','usagetype':usage}},
                'terms':{'OnDemand':{'offer':{'priceDimensions':{'rate':{'unit':unit,'beginRange':'0','endRange':'Inf','pricePerUnit':{'USD':price}}}}}}})]}
            prices.append({'kind':kind,'reference':self.put(kind,original)})
        self.quote={'schema':'honua.registrar-liability-quote/v1','candidateKey':self.candidate,'runtimeId':'runtime','region':'us-east-1',
            'cpu':256,'memory':1024,'maxExecutionSeconds':600,'image':self.image,'maximumUsd':self.config['maximumUsd'],
            'bounds':{'maximumImagePullBytes':2*1024**3,'maximumRequestResponseBytes':64*1024**2,'maximumLogBytes':1024**2,
                'maximumTaskStarts':1,'maximumCleanupTaskStarts':1,'cleanupMaxExecutionSeconds':600},'pricingOriginals':prices,
            'imageOriginal':self.put('image',{'schema':'honua.registrar-ecr-image-original/v1','region':'us-east-1','accountId':'123456789012',
                'repository':'honua-pilot-test-trusted-controller','imageDigest':'sha256:'+'c'*64,'response':{'imageDetails':[{
                    'registryId':'123456789012','repositoryName':'honua-pilot-test-trusted-controller','imageDigest':'sha256:'+'c'*64,'imageSizeInBytes':1024**3}]}}),
            'sharedInfrastructureOriginal':self.put('shared',{'schema':'honua.registrar-shared-infrastructure-binding/v1','candidateKey':self.candidate,
                'operationId':'original-network','basisDigest':'sha256:'+'f'*64,'reservationIds':{'infrastructure':'shared-infrastructure','cleanup':'shared-cleanup'}})}
        self.refresh_quote()
        self.funding=patch.object(admission,'validate_retained_funding');self.funding.start();self.addCleanup(self.funding.stop)
        self.budget=patch.object(admission,'validate_budget');self.budget.start();self.addCleanup(self.budget.stop)

    def put(self,name,value):
        path='certification/hosted-pilot/quotes/'+name+'.json';target=self.root/path;target.parent.mkdir(parents=True,exist_ok=True)
        raw=json.dumps(value,sort_keys=True,separators=(',',':')).encode();target.write_bytes(raw)
        return {'path':path,'sha256':'sha256:'+hashlib.sha256(raw).hexdigest()}

    def refresh_quote(self):
        self.config['liabilityQuote']=self.put('quote',self.quote)
        self.config['operation']={'operationId':'runtime-registration-runtime','reservationIds':{'infrastructure':'reg-infra','cleanup':'reg-cleanup'},
            'liabilityBasisDigest':canonical_digest({k:v for k,v in self.config.items() if k!='operation'})}

    def source(self,path,sha):return self.root/path
    def produce(self):return admission.retain_admission_and_reservations(self.config,self.durable,source_reader=self.source,guard=MagicMock(),clock=lambda:self.now)
    def verify(self):return admission.verify_retained_admission(self.config,self.durable,source_reader=self.source,clock=lambda:self.now)

    def test_admission_is_persisted_before_distinct_rows_and_resume_has_no_new_clock(self):
        original=self.produce();self.assertEqual(original['deadlineEpoch'],2000000600)
        self.assertEqual(len(self.persisted[0]['ledger']['operations']),2)
        self.assertEqual(self.durable.update.call_count,2)
        self.now+=50
        self.assertEqual(self.produce(),original);self.assertEqual(self.durable.update.call_count,2)

    def test_actual_history_invariant_forbids_rewriting_both_deadline_copies(self):
        from agent_e2e_state import preserve_history
        from test_native_runtime_registration_carrier import RegistrarCarrier
        # The maintained history guard also authenticates original funding.
        # Use complete retained originals and prove their unchanged form first.
        fixture=RegistrarCarrier()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        original=copy.deepcopy(fixture.durable.state)
        preserve_history(original,copy.deepcopy(original))
        changed=copy.deepcopy(original)
        changed['runtimeRegistrationAdmissions'][fixture.config['operation']['operationId']]['deadlineEpoch']+=1
        changed['scenarioOperations'][admission.admission_key(fixture.config)]['value']['deadlineEpoch']+=1
        with self.assertRaisesRegex(Exception,'rewrite or erase retained'):preserve_history(original,changed)

    def test_deleting_index_cannot_restart_original_custody_clock(self):
        self.produce();self.durable.state['runtimeRegistrationAdmissions'].clear();self.operations[:]=self.operations[:2]
        with self.assertRaisesRegex(ValueError,'orphan admission custody'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_read_only_guard_requires_original_immutable_custody(self):
        self.produce();self.durable.state['scenarioOperations'].clear()
        with self.assertRaisesRegex(ValueError,'append-only original custody'):self.verify()

    def test_original_funding_caps_absolute_deadline(self):
        self.durable.state['ledger']['interactiveAccounting']['originalExpiresEpoch']=self.now+120
        original=self.produce();self.assertEqual(original['deadlineEpoch'],self.now+120)

    def test_deadline_expiry_does_not_renew_or_reserve_again(self):
        original=self.produce();self.now=original['deadlineEpoch']
        with self.assertRaisesRegex(ValueError,'expired'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_missing_admission_never_uses_old_rows_as_new_start(self):
        self.produce();self.durable.state.pop('runtimeRegistrationAdmissions')
        with self.assertRaisesRegex(ValueError,'orphan admission custody'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_orphan_reservations_cannot_start_a_new_clock_without_either_admission_copy(self):
        self.produce();self.durable.state.pop('runtimeRegistrationAdmissions')
        self.durable.state['scenarioOperations'].pop(admission.admission_key(self.config))
        with self.assertRaisesRegex(ValueError,'orphan reservations'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_read_only_guard_cannot_create_missing_admission(self):
        with self.assertRaisesRegex(ValueError,'missing'):self.verify()
        self.durable.update.assert_not_called();self.durable._persist.assert_not_called()

    def test_changed_config_is_refused_before_partial_reservation_recovery(self):
        self.produce();self.operations.pop();self.config['maxExecutionSeconds']=599;self.quote['maxExecutionSeconds']=599;self.refresh_quote()
        with self.assertRaisesRegex(ValueError,'changed'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_missing_cleanup_row_is_recovered_once_under_original_admission(self):
        original=self.produce();self.operations.pop();self.now+=20
        recovered=self.produce();self.assertEqual(original,recovered);self.assertEqual(self.durable.update.call_count,3)
        self.assertEqual(len([r for r in self.operations if r['id']=='reg-cleanup']),1)

    def test_forged_extended_deadline_is_refused_before_missing_row_recovery(self):
        self.produce();self.operations.pop();self.durable.state['runtimeRegistrationAdmissions']['runtime-registration-runtime']['deadlineEpoch']+=1
        with self.assertRaisesRegex(ValueError,'changed'):self.produce()
        self.assertEqual(self.durable.update.call_count,2)

    def test_rehashed_quote_cannot_underfund_real_meter_calculation(self):
        self.quote['maximumUsd']=self.config['maximumUsd']={'infrastructure':'0.0001','cleanup':'0.0001'};self.refresh_quote()
        with self.assertRaisesRegex(ValueError,'does not cover'):self.produce()
        self.durable._persist.assert_not_called()

    def test_wrong_pricing_region_even_with_rehashed_source_is_refused(self):
        ref=self.quote['pricingOriginals'][0]['reference'];raw=json.loads((self.root/ref['path']).read_text())
        v=json.loads(raw['PriceList'][0]);v['product']['attributes']['regionCode']='us-west-2';raw['PriceList'][0]=json.dumps(v)
        self.quote['pricingOriginals'][0]['reference']=self.put('fargate-vcpu',raw);self.refresh_quote()
        with self.assertRaisesRegex(ValueError,'region/meter'):self.produce()
        self.durable.update.assert_not_called()

    def test_image_bytes_exceeding_reviewed_quote_refuse_before_admission(self):
        self.quote['bounds']['maximumImagePullBytes']=1024;self.refresh_quote()
        with self.assertRaisesRegex(ValueError,'transfer bound'):self.produce()
        self.durable._persist.assert_not_called()

    def test_new_rows_cannot_alias_original_shared_infrastructure(self):
        self.config['operation']['reservationIds']['infrastructure']='shared-infrastructure'
        with self.assertRaisesRegex(ValueError,'reuse'):self.produce()
        self.durable.update.assert_not_called()

    def test_fresh_original_head_must_match_actual_owner_fence_and_version(self):
        self.durable.store.db.get_item.return_value['Item']['stateVersion']['N']='8'
        with self.assertRaisesRegex(ValueError,'different live state head'):self.produce()
        self.durable._persist.assert_not_called()

    def test_read_only_guard_refuses_settled_or_underfunded_original_rows(self):
        self.produce();self.operations[-1]['amountUsd']='0.01'
        with self.assertRaisesRegex(ValueError,'quoted reservation'):self.verify()
        self.assertEqual(self.durable.update.call_count,2)


if __name__=='__main__':unittest.main()
