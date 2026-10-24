"""Only the authenticated original refused plan can remain funded diagnostic history."""
import base64
import copy
import json
import unittest
from unittest.mock import patch
import history_reconciliation as h
import history_failed_plan as failed
from agent_e2e_pack import canonical_digest,digest_bytes

# Exact allowlisted original journal copied from artifact12524278730.
ORIGINAL_JOURNAL = {'bootstrap_run_id': '40007575185',
 'cloud_discovery_complete': False,
 'cloud_discovery_tags': {'BootstrapConfigPath': 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json',
                          'BootstrapConfigSha256': 'ebb42c3be45e9efa7bec217628ac006cc43106f9172630c5d9b62691e90e5f09',
                          'BootstrapExecution': '40007575185',
                          'BootstrapFoundationSha': '2cf6e807e0fa7a80cf2e4f124085ee7aae4253c9',
                          'BootstrapLockSha256': '7fd30e70f88ed9c5de18a5d13a5d91303fe233a14719dede9d6ededd3a2d0851',
                          'BootstrapReleaseSha': '94fc5d304af840440c2d48c14186796418a0d2e0',
                          'ManagedBy': 'honua-release-hosted-pilot-bootstrap',
                          'ResetBoundary': 'honua-pilot-foss4g'},
 'config_path': 'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json',
 'config_sha256': 'ebb42c3be45e9efa7bec217628ac006cc43106f9172630c5d9b62691e90e5f09',
 'foundation_backend': {'bucket': 'honua-release-pilot-foss4g-foundation-state-585192672263',
                        'key': 'honua/hosted-pilot/honua-pilot-foss4g/foundation.tfstate'},
 'foundation_source_sha': '2cf6e807e0fa7a80cf2e4f124085ee7aae4253c9',
 'provider_lock_sha256': '7fd30e70f88ed9c5de18a5d13a5d91303fe233a14719dede9d6ededd3a2d0851',
 'release_root_sha': '94fc5d304af840440c2d48c14186796418a0d2e0',
 'remote_recovery_state': None,
 'resources': [],
 'schema': 'honua.hosted-pilot-custody-recovery/v1',
 'status': 'recovery-required-until-remote-state-verified',
 'unmapped_owned_key_ids': [],
 'unverified_bucket_names': []}
CONFIG_BYTES = base64.b64decode('ewogICJhY2NvdW50X2lkIjogIjU4NTE5MjY3MjI2MyIsCiAgImJ1ZGdldF9sZWRnZXIiOiB7CiAgICAiYWN0aXZlX3BhaWRfYXR0ZW1wdHMiOiAwLAogICAgImNsZWFudXBfcmVzZXJ2ZV91c2QiOiAyMDAsCiAgICAiY29uZmlnX3NoYTI1NiI6IG51bGwsCiAgICAiZGVwbG95bWVudF9yZXNlcnZhdGlvbl91c2QiOiA1LAogICAgImV4cGlyZXNfYXQiOiAiMjAyNi0xMC0yNFQxMzo0NTowMFoiLAogICAgInJlc2VydmF0aW9uX2lkIjogImZvdW5kYXRpb24taW1hZ2UtcHJlcGFyYXRpb24tMDAxIiwKICAgICJyZXNlcnZlZF91c2QiOiAzMzQsCiAgICAic2NoZW1hIjogImhvbnVhLmhvc3RlZC1waWxvdC1idWRnZXQvdjEiLAogICAgInNvdXJjZV9zaGEiOiBudWxsLAogICAgInNwZW50X3VzZCI6IDAsCiAgICAidG90YWxfYnVkZ2V0X3VzZCI6IDIwMDAKICB9LAogICJjb250cm9sbGVyX21vZGVsX3Jlc291cmNlX2FybnMiOiBbXSwKICAiZW5hYmxlX2FnZW50X2NvbnRyb2xsZXIiOiB0cnVlLAogICJleHBpcmVzX2F0IjogIjIwMjYtMTAtMzBUMDA6MDA6MDBaIiwKICAiaW1hZ2VfcHJlcGFyYXRpb25fZnVuZGluZyI6IHRydWUsCiAgIm5hbWVfcHJlZml4IjogImhvbnVhLXBpbG90LWZvc3M0ZyIsCiAgIm9pZGNfcHJvdmlkZXJfYXJuIjogImFybjphd3M6aWFtOjo1ODUxOTI2NzIyNjM6b2lkYy1wcm92aWRlci90b2tlbi5hY3Rpb25zLmdpdGh1YnVzZXJjb250ZW50LmNvbSIsCiAgIm93bmVyIjogIk1pa2UgTWNEb3VnYWxsIiwKICAicHJvdmlzaW9uX3BvbGljeV9kb2N1bWVudCI6ICJ7XCJWZXJzaW9uXCI6XCIyMDEyLTEwLTE3XCIsXCJTdGF0ZW1lbnRcIjpbe1wiU2lkXCI6XCJDYW5kaWRhdGVJbmRlcGVuZGVudEltYWdlUHJlcGFyYXRpb25Pbmx5XCIsXCJFZmZlY3RcIjpcIkFsbG93XCIsXCJBY3Rpb25cIjpbXCJlY3I6RGVzY3JpYmVSZXBvc2l0b3JpZXNcIixcImVjcjpHZXRMaWZlY3ljbGVQb2xpY3lcIl0sXCJSZXNvdXJjZVwiOltcImFybjphd3M6ZWNyOnVzLWVhc3QtMTo1ODUxOTI2NzIyNjM6cmVwb3NpdG9yeS9ob251YS1waWxvdC1mb3NzNGctdHJ1c3RlZC1jb250cm9sbGVyXCIsXCJhcm46YXdzOmVjcjp1cy1lYXN0LTE6NTg1MTkyNjcyMjYzOnJlcG9zaXRvcnkvaG9udWEtcGlsb3QtZm9zczRnLXdvcmtlclwiXX1dfSIsCiAgInJlZ2lvbiI6ICJ1cy1lYXN0LTEiLAogICJydW50aW1lX2ttc19rZXlfYXJucyI6IFtdLAogICJydW50aW1lX3NlY3JldF9hcm5zIjogWwogICAgImFybjphd3M6c2VjcmV0c21hbmFnZXI6dXMtZWFzdC0xOjU4NTE5MjY3MjI2MzpzZWNyZXQ6aG9udWEtcmVsZWFzZS9jZXJ0L29wZXJhdGlvbi1rZXktcmluZy16RG42MTgiLAogICAgImFybjphd3M6c2VjcmV0c21hbmFnZXI6dXMtZWFzdC0xOjU4NTE5MjY3MjI2MzpzZWNyZXQ6aG9udWEtcmVsZWFzZS9jZXJ0L2F1ZGl0LWNoYWluLWtleS1STHRwTUUiCiAgXQp9Cg==')

class RefusedPlanHistory(unittest.TestCase):
    def setUp(self):
        p=failed.PRODUCER
        self.record={'schema':'honua.hosted-pilot-producer-reconciliation/v1','kind':'foundation-plan-refused',
            'producer':copy.deepcopy(p),'fundingBasis':{'path':'basis','sha256':'sha256:'+'a'*64},
            'fundingReservationIds':['foundation-infrastructure','foundation-cleanup'],'ownerLedger':None,
            'artifact':copy.deepcopy(failed.ARTIFACT),'planArtifact':None,'cloudEvents':[]}
        self.run={'id':p['runId'],'run_attempt':1,'head_sha':p['sourceSha'],'path':p['workflow'],
            'head_branch':'trunk','event':'workflow_dispatch','status':'completed','conclusion':'failure'}
        common={'head_sha':p['sourceSha'],'run_id':p['runId'],'status':'completed','completed_at':'2026-10-23T19:00:40Z'}
        self.jobs={'total_count':2,'jobs':[
            {**common,'name':'approval-policy','id':failed.APPROVAL_JOB_ID,'conclusion':'success','steps':[]},
            {**common,'name':'bootstrap','id':failed.BOOTSTRAP_JOB_ID,'conclusion':'failure','steps':[
                {'name':name,'status':'completed','conclusion':value} for name,value in {
                    'Verify exact candidate-independent configuration and release compiler before AWS credentials':'success',
                    'Custody-first source-reviewed bootstrap in one ephemeral job':'failure',
                    'Preserve initial-custody recovery identities after failure or cancellation':'success',
                    'Remove private executor material':'success'}.items()]}]}
        self.receipt=copy.deepcopy(ORIGINAL_JOURNAL);self.protected=True;self.calls=[]
        self.log=('2026-10-23T18:58:00.000Z ##[group]Run python3 tools/hosted_pilot/bootstrap.py "$MODE" --config "$CONFIG"\n'+
            '\n'.join('2026-10-23T18:58:00.001Z   '+key+': '+value for key,value in {
                'MODE':'plan','CONFIG':failed.CONFIG_PATH,'PILOT_RELEASE_ROOT_SHA':failed.RELEASE_SHA,'AWS_REGION':'us-east-1'}.items())+
            '\n2026-10-23T18:58:00.002Z ##[endgroup]\n').encode()
    def native(self,argv):
        path=argv[-1];self.calls.append(path)
        if path.endswith('/logs'):return self.log
        if '/branches/trunk' in path:value={'protected':self.protected}
        elif '/compare/' in path:value={'merge_base_commit':{'sha':failed.RELEASE_SHA if '/honua-release/' in path else failed.PRODUCER['sourceSha']}}
        elif '/jobs?' in path:value=self.jobs
        elif '/artifacts?' in path:value={'total_count':1,'artifacts':[failed.ARTIFACT]}
        else:value=self.run
        return json.dumps(value).encode()
    def authenticate(self):
        with patch.object(h,'verify_sources') as sources,patch.object(h,'source_file',return_value=CONFIG_BYTES),                patch.object(h,'native_artifact',return_value=self.receipt):
            value=h.authenticate(self.record,call=self.native)
            self.assertEqual(sources.call_count,2)
            return value
    def test_positive_preserves_native_failure_incomplete_discovery_and_absent_actor(self):
        before=copy.deepcopy((self.record,self.run,self.receipt))
        run,jobs,receipt,sha=self.authenticate()
        self.assertEqual(run['conclusion'],'failure');self.assertFalse(receipt['cloud_discovery_complete'])
        self.assertNotIn('execution_identity',receipt);self.assertEqual(sha,failed.RELEASE_SHA)
        self.assertEqual((self.record,self.run,self.receipt),before)
    def test_other_runs_attempts_sources_and_model_workflows_are_never_admitted(self):
        for key,value in [('runId',failed.PRODUCER['runId']+1),('runAttempt',2),('sourceSha','f'*40),
                ('workflow','.github/workflows/terminal-model-canary.yml')]:
            self.setUp();self.record['producer'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):self.authenticate()
            self.assertEqual(self.calls,[])
    def test_missing_altered_artifact_owner_or_claimed_resources_refuse(self):
        for key,value in [('artifact',failed.ARTIFACT|{'sha256':'sha256:'+'f'*64}),('ownerLedger',{'path':'owner'}),
                ('planArtifact',{'id':1}),('cloudEvents',[{'eventName':'CreateBucket'}])]:
            self.setUp();self.record[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'cannot claim'):self.authenticate()
    def test_no_success_cancellation_or_unstopped_job_substitution(self):
        for mutation in ('success','cancelled','running','another-failure','source','unprotected'):
            self.setUp()
            if mutation in ('success','cancelled'):self.run['conclusion']=mutation
            elif mutation=='running':self.jobs['jobs'][1]['status']='in_progress'
            elif mutation=='another-failure':self.jobs['jobs'][1]['steps'][0]['conclusion']='failure'
            elif mutation=='source':self.jobs['jobs'][1]['head_sha']='f'*40
            else:self.protected=False
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.authenticate()
    def test_incomplete_discovery_cannot_be_rewritten_as_complete_or_empty_settlement(self):
        for key,value in [('cloud_discovery_complete',True),('resources',[{'id':'unresolved'}]),
                ('remote_recovery_state',{'sha256':'guessed'}),('unmapped_owned_key_ids',['unknown'])]:
            self.setUp();self.receipt[key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'incomplete discovery changed'):self.authenticate()
    def test_deploy_wrong_config_and_output_only_mode_markers_refuse(self):
        for mutation in ('deploy','config','output-only','duplicate'):
            self.setUp()
            if mutation=='deploy':self.log=self.log.replace(b'MODE: plan',b'MODE: deploy')
            elif mutation=='config':self.log=self.log.replace(failed.CONFIG_PATH.encode(),b'foreign-config')
            elif mutation=='duplicate':self.log+=self.log
            else:self.log=self.log.replace(b'  MODE: plan',b'  UNREVIEWED: plan')+b'  MODE: plan\n'
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):self.authenticate()
    def test_only_exact_public_runner_fields_survive_log_projection(self):
        log=self.log.replace(b'##[endgroup]',b'  PRIVATE_CREDENTIAL: sentinel\n##[endgroup]')
        fields=failed.plan_header(log)
        self.assertEqual(set(fields),{'MODE','CONFIG','PILOT_RELEASE_ROOT_SHA','AWS_REGION'})
        self.assertNotIn('sentinel',json.dumps(fields))
    def reconcile(self,ledger,cutoff='2026-10-23T19:01:00Z'):
        basis={'approved':'nonzero original budget'};raw=json.dumps(basis).encode()
        record=copy.deepcopy(self.record);record['fundingBasis']={'path':'basis','sha256':digest_bytes(raw)}
        values={'basis':basis,'record':record}
        history={'repositories':{'honua-io/honua-iac':{'producerRuns':[{'id':failed.PRODUCER['runId'],'run_attempt':1,
            'head_sha':failed.PRODUCER['sourceSha'],'path':failed.PRODUCER['workflow'],'conclusion':'failure'}]}},'cloudTrail':{'RunTask':{'pilotEvents':[]}}}
        before=copy.deepcopy((history,ledger))
        def file(repo,sha,path,call):return raw if path=='basis' else CONFIG_BYTES
        with patch.object(h,'verify_sources'),patch.object(h,'source_file',side_effect=file),patch.object(h,'native_artifact',return_value=self.receipt):
            h.reconcile(history,ledger,[{'path':'record'}],read=lambda ref:values[ref['path']],cutoff=cutoff,call=self.native)
        self.assertEqual((history,ledger),before)
    def ledger(self):
        basis={'approved':'nonzero original budget'}
        return {'candidateKey':'sha256:'+'a'*64,'knownIncurredUsd':'157.0291032026','operations':[
            {'id':name,'amountUsd':amount,'kind':kind,'purpose':'bootstrap','basisDigest':canonical_digest(basis),
             'state':'reserved','actualUsd':None} for name,amount,kind in [('foundation-infrastructure','25','infrastructure'),('foundation-cleanup','10','cleanup')]]}
    def test_reconciliation_retains_nonzero_original_reservations_and_failed_report(self):self.reconcile(self.ledger())
    def test_zero_missing_cleanup_and_after_cutoff_refuse_without_settlement(self):
        for cut in ('zero','missing','settled-zero','after-cutoff'):
            ledger=self.ledger()
            if cut=='zero':ledger['operations'][0]['amountUsd']='0'
            elif cut=='missing':ledger['operations'].pop()
            elif cut=='settled-zero':ledger['operations'][1].update(state='settled',actualUsd='0')
            with self.subTest(cut=cut),self.assertRaises(ValueError):
                self.reconcile(ledger,'2026-10-23T18:59:00Z' if cut=='after-cutoff' else '2026-10-23T19:01:00Z')

    def test_only_the_observed_successful_plan_can_keep_its_original_source_contract(self):
        record={'kind':'foundation-plan','producer':copy.deepcopy(failed.SUCCESS_PRODUCER),'artifact':copy.deepcopy(failed.SUCCESS_ARTIFACT)}
        receipt={'release_root_sha':failed.SUCCESS_RELEASE_SHA,'phase':'custody-only',
            'plan_sha256':'be3d7edfae7fab75fc565a8073168f469ed173f0cf619bd1199b7c23fa69a2fd'}
        self.assertEqual(failed.original_success_sources(record,receipt),(failed.IAC_SOURCES,failed.SUCCESS_RELEASE_SOURCES))
        changed=copy.deepcopy(record);changed['producer']['runId']+=1
        self.assertIsNone(failed.original_success_sources(changed,receipt))
        for field,value in [('phase','remote-backed-foundation'),('release_root_sha','f'*40),('plan_sha256','f'*64)]:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'original successful custody plan'):
                failed.original_success_sources(record,receipt|{field:value})
        record['artifact']['sha256']='sha256:'+'f'*64
        with self.assertRaisesRegex(ValueError,'original successful custody plan'):
            failed.original_success_sources(record,receipt)
