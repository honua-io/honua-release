"""Read-only SDK-shape original authentication; no fake live lease or launch."""
import copy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock

from agent_e2e_pack import canonical_digest, digest_bytes
from registration_allowance import admission_key
from registration_dispatch_admission import verify_original_dispatch_admission
import test_native_runtime_registration_carrier as fixture_source


class OriginalDispatchAdmission(unittest.TestCase):
    def setUp(self):
        # Reuse the real authenticated original-funding fixture, not a mocked
        # budget or retained-source verifier. Its own tests are not rerun here.
        self.fixture=fixture_source.RegistrarCarrier();self.addCleanup(self.fixture.doCleanups);self.fixture.setUp()
        f=self.fixture;self.config=copy.deepcopy(f.config);self.payload=copy.deepcopy(f.durable.state)
        marker={'schemaVersion':1,'candidateKey':self.config['candidateKey'],'generationId':'original-generation'}
        marker_raw=self.encoded(marker);generation=digest_bytes(marker_raw)
        self.config['generationDigest']=generation
        basis=canonical_digest({k:v for k,v in self.config.items() if k!='operation'})
        self.config['operation']['liabilityBasisDigest']=basis
        for row in self.payload['ledger']['operations']:
            if row['operationId']==self.config['operation']['operationId']:row['basisDigest']=basis
        row=self.payload['runtimeRegistrationAdmissions'][self.config['operation']['operationId']]
        row.update(configurationDigest=canonical_digest(self.config),basisDigest=basis)
        row['sourceHead']['generationDigest']=generation
        self.payload['scenarioOperations'][admission_key(self.config)]['value']=copy.deepcopy(row)
        self.expected=copy.deepcopy(row)
        prefix='agent-e2e/'+self.config['candidateKey'][7:]+'/'
        self.snapshot={'candidateKey':self.config['candidateKey'],'generationDigest':generation,'version':8,'payload':self.payload}
        snapshot_raw=self.encoded(self.snapshot)
        self.head={'generationKey':{'S':prefix+'initialization.json'},'generationVersion':{'S':'generation-v1'},
            'generationDigest':{'S':generation},'snapshotKey':{'S':prefix+'snapshot-8.json'},
            'snapshotVersion':{'S':'snapshot-v8'},'snapshotDigest':{'S':digest_bytes(snapshot_raw)},'stateVersion':{'N':'8'}}
        self.objects={(prefix+'initialization.json','generation-v1'):marker_raw,(prefix+'snapshot-8.json','snapshot-v8'):snapshot_raw}
        db=MagicMock();db.get_item.side_effect=lambda **kw:{'Item':copy.deepcopy(self.head)}
        s3=MagicMock()
        def read(**kw):
            raw=self.objects[(kw['Key'],kw['VersionId'])]
            return {'VersionId':kw['VersionId'],'ContentLength':len(raw),'Body':io.BytesIO(raw)}
        s3.get_object.side_effect=read
        self.store=SimpleNamespace(db=db,s3=s3,candidate=self.config['candidateKey'],
            bucket=self.config['state']['bucket'],table=self.config['state']['table'],key={'candidateKey':{'S':self.config['candidateKey']}})
        self.now=f.admitted+1

    @staticmethod
    def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':')).encode()

    def verify(self):
        return verify_original_dispatch_admission(self.config,self.store,source_reader=self.fixture.source,clock=lambda:self.now)

    def test_actual_original_versions_and_stable_head_authenticate_without_a_lease(self):
        result=self.verify();self.assertEqual(result['admission'],self.expected);self.assertEqual(result['head'],self.head)
        self.assertEqual(self.store.db.get_item.call_count,2);self.assertEqual(self.store.s3.get_object.call_count,2)
        self.store.db.update_item.assert_not_called();self.store.db.put_item.assert_not_called();self.store.db.transact_write_items.assert_not_called()
        self.assertFalse(hasattr(self.store,'_require_live_lease'))

    def test_absent_head_does_not_initialize_or_reserve(self):
        self.store.db.get_item.side_effect=lambda **kw:{}
        with self.assertRaisesRegex(Exception,'absent or incomplete'):self.verify()
        self.store.s3.get_object.assert_not_called();self.store.db.update_item.assert_not_called()

    def test_changed_native_head_during_object_reads_is_refused(self):
        changed=copy.deepcopy(self.head);changed['snapshotVersion']={'S':'another-version'}
        self.store.db.get_item.side_effect=[{'Item':self.head},{'Item':changed}]
        with self.assertRaisesRegex(Exception,'head advanced'):self.verify()

    def test_original_snapshot_byte_mismatch_is_refused(self):
        key=(self.head['snapshotKey']['S'],self.head['snapshotVersion']['S']);self.objects[key]=b'{}'
        with self.assertRaisesRegex(Exception,'digest or length'):self.verify()

    def test_source_configuration_cannot_label_another_generation(self):
        self.config['generationDigest']='sha256:'+'0'*64
        self.config['operation']['liabilityBasisDigest']=canonical_digest({k:v for k,v in self.config.items() if k!='operation'})
        with self.assertRaisesRegex(ValueError,'generation'):self.verify()

    def test_live_funding_does_not_renew_expired_original_registration_deadline(self):
        self.now=self.expected['deadlineEpoch']
        with self.assertRaisesRegex(ValueError,'expired'):self.verify()


if __name__=='__main__':unittest.main()
