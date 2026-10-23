import copy
from datetime import timedelta
import hashlib
import json
import unittest

import history_phase
import history_reconciliation as history
from test_bootstrap_phase import Native, ENV, NOW
import bootstrap_phase as phase


class OriginalPhaseHistory(unittest.TestCase):
    def fixture(self, selected='custody-only'):
        self.native=Native();n=self.native
        n.public['phase']=selected;n.rows=[];n.zips={}
        reference=n.add(phase.names(selected,ENV)[0],'review.json',n.public)
        n.receipt['review_reference']=reference
        approval_reference=n.add(phase.names(selected,ENV)[1],'approved.json',n.receipt)
        self.proof={'reference':approval_reference,'receipt':n.receipt,'approval_job_id':3}
        for row in n.jobs['jobs']:
            row.update(status='completed',conclusion='success',started_at=(NOW-timedelta(minutes=1)).isoformat(),completed_at=(NOW+timedelta(minutes=2)).isoformat())
        n.jobs['jobs'].append({'id':4,'name':'phase-watchdog','head_sha':'a'*40,'run_id':123,'status':'completed','conclusion':'success','completed_at':(NOW+timedelta(minutes=10)).isoformat()})
        n.jobs['total_count']=4
        steps=[]
        for name,start,end in [('Prepare exactly one source-reviewed phase without applying',-1,1),('Wait for original exact-plan approval from the separate protected job',1,3),('Apply only the exact reviewed phase and stop',3,8)]:
            steps.append({'name':name,'status':'completed','conclusion':'success','started_at':(NOW+timedelta(minutes=start)).isoformat(),'completed_at':(NOW+timedelta(minutes=end)).isoformat()})
        n.jobs['jobs'][0]['steps']=steps
        self.admission={'schema':'honua.hosted-pilot-phase-admitted/v1','review':n.public,'approval':self.proof,'admitted_at':(NOW+timedelta(minutes=4)).isoformat(),'financial_settlement':False,'partial_failure_liability_retained':True}
        n.add(f'hosted-pilot-phase-admitted-123-2-{selected}','apply-admitted.json',self.admission)
        self.record={'kind':'foundation-custody' if selected=='custody-only' else 'foundation','producer':{'repository':phase.REPOSITORY,'runId':123,'runAttempt':2,'sourceSha':'a'*40,'workflow':phase.WORKFLOW},
            'planArtifact':{'id':1,'name':n.rows[0]['name'],'sha256':n.rows[0]['digest']},'ownerLedger':None,'cloudEvents':[]}
        self.receipt={'schema':'honua.hosted-pilot-custody-applied/v1','review':n.public,'approval':self.proof,'apply_admission_sha256':hashlib.sha256(phase.canonical(self.admission)).hexdigest()}
        if selected!='custody-only':self.receipt={'schema':'honua.hosted-pilot-bootstrap-bindings/v2','phase_review':n.public,'phase_approval':self.proof,'apply_admission_sha256':self.receipt['apply_admission_sha256']}

    def setUp(self):self.fixture()

    def call(self,argv):
        path=argv[-1]
        if '/actions/artifacts/' in path and not path.endswith('/zip'):
            identifier=int(path.split('/')[-1]);return json.dumps(next(row for row in self.native.rows if row['id']==identifier)).encode()
        value=self.native(path)
        if '/artifacts?' in path:value['total_count']=len(value['artifacts'])
        return value if isinstance(value,bytes) else json.dumps(value).encode()

    def observe(self):
        policy={'reviewers':[{'type':'User','id':12301237,'login':'mikemcdougall'}],'environment':'terraform-live-approval'}
        return history_phase.applied(self.record,self.receipt,self.native.jobs,native_artifact=history.native_artifact,api=history.api,
            source_file=lambda *args:json.dumps(policy).encode(),call=self.call)

    def test_authentic_custody_protocol_preserves_original_phase_and_actor_shape(self):
        original=copy.deepcopy(self.record)
        result=self.observe()
        self.assertEqual(result['phase'],'custody-only');self.assertEqual(result['foundation_run_id'],'123')
        self.assertEqual(self.record,original)
        self.assertIsNone(self.record['ownerLedger'])

    def test_full_foundation_uses_distinct_original_phase_protocol(self):
        self.fixture('remote-backed-foundation')
        self.assertEqual(self.observe(),self.receipt)

    def test_approval_artifact_hash_and_native_job_substitution_refuse(self):
        for field,value in [('approval_job_id',900),('reference',dict(self.proof['reference'],artifact_sha256='0'*64))]:
            with self.subTest(field=field):
                original=self.proof[field];self.proof[field]=value
                with self.assertRaises(ValueError):self.observe()
                self.proof[field]=original

    def test_actual_human_approval_history_is_mandatory(self):
        for rows in [[],[dict(self.native.approvals[0],state='rejected')],[dict(self.native.approvals[0],user={'id':1,'login':'foreign','type':'User'})]]:
            original=self.native.approvals;self.native.approvals=rows
            with self.assertRaises(ValueError):self.observe()
            self.native.approvals=original

    def test_no_renewed_deadline_or_late_native_approval(self):
        gate=self.native.jobs['jobs'][2];original=gate['completed_at']
        gate['completed_at']=(NOW+timedelta(minutes=21)).isoformat()
        with self.assertRaises(ValueError):self.observe()
        gate['completed_at']=original
        execution=self.native.jobs['jobs'][0]['steps'][2]
        execution['completed_at']=(NOW+timedelta(minutes=36)).isoformat()
        with self.assertRaises(ValueError):self.observe()

    def test_original_admission_digest_and_liability_custody_are_required(self):
        self.receipt['apply_admission_sha256']='0'*64
        with self.assertRaises(ValueError):self.observe()

    def test_custody_cannot_claim_an_owner_ledger_or_other_phase(self):
        self.record['ownerLedger']={'path':'invented'}
        with self.assertRaises(ValueError):self.observe()
        self.record['ownerLedger']=None;self.native.public['phase']='remote-backed-foundation'
        with self.assertRaises(ValueError):self.observe()

    def test_v2_plan_requires_positive_native_skipped_apply_and_no_effect_artifacts(self):
        self.record.update(kind='foundation-plan',artifact=self.record['planArtifact'],planArtifact=None)
        for row in self.native.jobs['jobs'][0]['steps'][1:]:row['conclusion']='skipped'
        for row in self.native.jobs['jobs'][1:]:row['conclusion']='skipped'
        history_phase.plan_only(self.record,self.native.public,self.native.jobs,self.native.rows[:1])
        with self.assertRaises(ValueError):history_phase.plan_only(self.record,self.native.public,self.native.jobs,self.native.rows)
        self.native.jobs['jobs'][0]['steps'][2]['conclusion']='success'
        with self.assertRaises(ValueError):history_phase.plan_only(self.record,self.native.public,self.native.jobs,self.native.rows[:1])

    def test_preparation_cannot_be_moved_outside_original_native_step(self):
        self.native.jobs['jobs'][0]['steps'][0]['completed_at']=(NOW-timedelta(minutes=2)).isoformat()
        with self.assertRaises(ValueError):self.observe()


if __name__=='__main__':unittest.main()
