import base64
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap
import bootstrap_phase
import custody_completion
import history_reconciliation as history
from test_bootstrap_phase import ENV, NOW
import test_history_phase as phase_fixtures


class RecoveryContinuation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.work=Path(self.temp.name)
        fixture=phase_fixtures.OriginalPhaseHistory();fixture.fixture();self.native=fixture.native
        self.config_path='certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
        self.raw_config=(history.ROOT/self.config_path).read_bytes();self.cfg=json.loads(self.raw_config)
        self.lock=(history.ROOT/'certification/hosted-pilot/bootstrap/.terraform.lock.hcl').read_bytes()
        self.old={'run_id':'99','identity':{'foundation_source_sha':'f'*40,'release_root_sha':'e'*40,'config_path':self.config_path,
            'config_sha256':hashlib.sha256(self.raw_config).hexdigest(),'provider_lock_sha256':hashlib.sha256(self.lock).hexdigest()}}
        self.tags=bootstrap.creation_tags(self.old['identity'],'99')|{'ResetBoundary':self.cfg['name_prefix']}
        self.binding={'bucket':f'honua-release-pilot-foss4g-foundation-state-{history.ACCOUNT}','key':f'honua/hosted-pilot/{self.cfg["name_prefix"]}/foundation.tfstate','region':'us-east-1','encrypt':True,'use_lockfile':True}
        self.key_arn=f'arn:aws:kms:us-east-1:{history.ACCOUNT}:key/11111111-1111-1111-1111-111111111111'
        self.identity={**self.old['identity'],'foundation_source_sha':'a'*40,'release_root_sha':'b'*40}
        n=self.native;review=n.public
        review.update(self.identity,original_creation=copy.deepcopy(self.old))
        review['execution_identity']=bootstrap.execution_identity({'Account':history.ACCOUNT,'Arn':f'arn:aws:iam::{history.ACCOUNT}:user/fixture','UserId':'AIDAFIXTURE'},self.identity,history.ACCOUNT,'123','2')
        review['creation_resources']=[]
        n.run.update(path=bootstrap_phase.WORKFLOW,status='completed',conclusion='success')
        n.rows=[];n.zips={}
        ref=n.add(bootstrap_phase.names('custody-only',ENV)[0],'review.json',review)
        n.receipt.update(review=review,review_reference=ref)
        approval_ref=n.add(bootstrap_phase.names('custody-only',ENV)[1],'approved.json',n.receipt)
        proof={'reference':approval_ref,'receipt':n.receipt,'approval_job_id':3}
        admission=fixture.admission|{'review':review,'approval':proof}
        n.add('hosted-pilot-phase-admitted-123-2-custody-only','apply-admitted.json',admission)
        self.receipt={'schema':'honua.hosted-pilot-custody-applied/v1','review':review,'approval':proof,'original_creation':copy.deepcopy(self.old),
            'apply_admission_sha256':hashlib.sha256(bootstrap_phase.canonical(admission)).hexdigest()}
        n.add('hosted-pilot-custody-applied-123-2','custody-applied.json',self.receipt)
        self.blob=bootstrap_phase.canonical(self.receipt);self.aws_calls=[];self.native_calls=[]
        self.addCleanup(patch.stopall)
        patch.dict(os.environ,ENV|{'PILOT_BOOTSTRAP_ACCOUNT_ID':history.ACCOUNT},clear=True).start()
        patch.object(bootstrap,'aws',side_effect=self.aws).start()
        patch.object(history,'invoke',side_effect=self.call).start()
        patch.object(bootstrap_phase,'api',side_effect=lambda path,**kw:json.loads(self.call(['gh','api',path]))).start()
        patch.object(bootstrap,'run',side_effect=lambda args,**kw:self.lock if args[-1].endswith('.terraform.lock.hcl') else self.raw_config if args[0]=='git' and 'show' in args else b'').start()

    def durable_key(self):
        return custody_completion.prefix(self.cfg,self.old)+hashlib.sha256(self.blob).hexdigest()+'.json'

    def aws(self,*args):
        self.aws_calls.append(args)
        if args[:2]==('s3api','get-bucket-tagging'):return {'TagSet':[{'Key':k,'Value':v} for k,v in self.tags.items()]}
        if args[:2]==('kms','list-resource-tags'):return {'Tags':[{'TagKey':k,'TagValue':v} for k,v in self.tags.items()]}
        if args[:2]==('s3api','get-bucket-encryption'):return {'ServerSideEncryptionConfiguration':{'Rules':[{'ApplyServerSideEncryptionByDefault':{'SSEAlgorithm':'aws:kms','KMSMasterKeyID':self.key_arn}}]}}
        if args[:2]==('s3api','get-public-access-block'):return {'PublicAccessBlockConfiguration':{key:True for key in ['BlockPublicAcls','IgnorePublicAcls','BlockPublicPolicy','RestrictPublicBuckets']}}
        if args[:2]==('s3api','get-bucket-versioning'):return {'Status':'Enabled'}
        if args[:2]==('s3api','list-objects-v2'):return {'Contents':[{'Key':self.durable_key()}],'IsTruncated':False}
        if args[:2]==('s3api','head-object'):return {'VersionId':'fixture-version','ContentLength':len(self.blob),'ServerSideEncryption':'aws:kms','SSEKMSKeyId':self.key_arn}
        if args[:2]==('s3api','get-object'):
            Path(args[-1]).write_bytes(self.blob);return {'VersionId':'fixture-version'}
        if args[:2]==('s3api','put-object'):
            self.blob=Path(args[args.index('--body')+1]).read_bytes()
            return {'VersionId':'fixture-version'}
        raise AssertionError(args)

    def call(self,argv):
        self.native_calls.append(argv);path=argv[-1];base='repos/honua-io/'
        if path.endswith('/actions/runs/99'):
            return json.dumps({'id':99,'workflow_id':88,'head_sha':'f'*40,'head_branch':'trunk','event':'workflow_dispatch','status':'completed','conclusion':'failure'}).encode()
        if path.endswith('/branches/trunk'):return b'{"protected":true}'
        if '/compare/' in path:return json.dumps({'merge_base_commit':{'sha':path.split('/compare/')[1].split('...')[0]}}).encode()
        if '/contents/' in path:
            relative=path.split('/contents/')[1].split('?ref=')[0]
            source=relative
            if path.startswith(base+'honua-iac/'):
                if relative==bootstrap_phase.WORKFLOW:source='certification/hosted-pilot/bootstrap/foundation-bootstrap-workflow.yml'
                elif relative=='scripts/check-hosted-pilot-foundation-inputs.py':source='certification/hosted-pilot/bootstrap/foundation-input-check.py'
            raw=(history.ROOT/source).read_bytes()
            return json.dumps({'type':'file','path':relative,'encoding':'base64','content':base64.b64encode(raw).decode()}).encode()
        if '/actions/artifacts/' in path and not path.endswith('/zip'):
            identifier=int(path.split('/')[-1]);value=next(row for row in self.native.rows if row['id']==identifier)
        else:value=self.native(path)
        if '/artifacts?' in path:value['total_count']=len(value['artifacts'])
        return value if isinstance(value,bytes) else json.dumps(value).encode()

    def observe(self):
        return bootstrap.observe_original_custody(self.binding,self.identity,self.cfg,history.ROOT,self.work)

    def test_failed_creator_successful_recovery_continues_with_real_authenticator(self):
        original=copy.deepcopy(self.tags);ledger=copy.deepcopy(self.cfg['budget_ledger'])
        result=self.observe()
        self.assertEqual(result,{k:self.tags[k] for k in ['ManagedBy','BootstrapExecution',*bootstrap.IDENTITY_TAGS.values()]})
        proof=json.loads((self.work/'original-successful-custody.json').read_text())
        self.assertEqual(proof['original_creation'],self.old)
        self.assertEqual(proof['successful_recovery']['producer']['runId'],123)
        self.assertFalse(proof['qualification']);self.assertFalse(proof['financial_settlement'])
        self.assertEqual(self.tags,original);self.assertEqual(self.cfg['budget_ledger'],ledger)
        self.assertEqual(json.loads(self.call(['gh','api','repos/honua-io/honua-iac/actions/runs/99']))['conclusion'],'failure')
        self.assertTrue(any('/approvals' in argv[-1] for argv in self.native_calls))
        self.assertFalse(any(args[1].startswith(('put','delete')) for args in self.aws_calls))

    def test_rehashed_forged_original_creator_refuses(self):
        bad=copy.deepcopy(self.receipt);bad['original_creation']['run_id']='98';self.blob=bootstrap_phase.canonical(bad)
        with self.assertRaises(ValueError):self.observe()

    def test_rehashed_durable_body_cannot_replace_native_original(self):
        bad=copy.deepcopy(self.receipt);bad['unreviewed']='forged';self.blob=bootstrap_phase.canonical(bad)
        with self.assertRaises(ValueError):self.observe()

    def test_recovery_still_requires_actual_human_review(self):
        self.native.approvals=[]
        with self.assertRaises(ValueError):self.observe()

    def test_recovery_job_failure_cannot_become_success(self):
        self.native.jobs['jobs'][2]['conclusion']='failure'
        with self.assertRaises(ValueError):self.observe()

    def test_failed_completion_retained_but_never_treated_as_success(self):
        self.native.run['conclusion']='failure'
        with self.assertRaises(ValueError):self.observe()
        self.assertEqual(self.native.run['conclusion'],'failure')

    def test_completion_uses_original_scope_conditional_encrypted_write(self):
        bootstrap.private_json(self.work/'custody-applied.json',self.receipt)
        secured=self.binding|{'kms_key_id':self.key_arn}
        result=custody_completion.retain(self.receipt,self.cfg,secured,self.work,aws=self.aws,exists=lambda *args:False)
        write=next(args for args in self.aws_calls if args[:2]==('s3api','put-object'))
        self.assertEqual(write[write.index('--if-none-match')+1],'*')
        self.assertEqual(write[write.index('--server-side-encryption')+1],'aws:kms')
        self.assertEqual(write[write.index('--ssekms-key-id')+1],self.key_arn)
        self.assertIn('/99/completions/',result['key']);self.assertNotIn('/123/completions/',result['key'])
        self.assertEqual(result['version_id'],'fixture-version')

    def test_lost_write_ack_never_deletes_or_replaces_original_completion(self):
        bootstrap.private_json(self.work/'custody-applied.json',self.receipt)
        secured=self.binding|{'kms_key_id':self.key_arn};stored=[]
        def lost(*args):
            if args[:2]==('s3api','put-object'):
                stored.append((args[args.index('--key')+1],Path(args[args.index('--body')+1]).read_bytes()))
                self.blob=stored[-1][1]
                raise ValueError('simulated acknowledgement loss')
            return self.aws(*args)
        with self.assertRaises(ValueError):
            custody_completion.retain(self.receipt,self.cfg,secured,self.work,aws=lost,exists=lambda *args:False)
        self.assertEqual(len(stored),1)
        self.assertEqual(stored[0][0],custody_completion.prefix(self.cfg,self.old)+bootstrap.digest(self.work/'custody-applied.json')+'.json')
        result=custody_completion.retain(self.receipt,self.cfg,secured,self.work,aws=self.aws,exists=lambda *args:True)
        self.assertEqual(result['key'],stored[0][0])
        self.assertFalse(any(args[:2]==('s3api','put-object') or args[1].startswith('delete') for args in self.aws_calls))


class ObservationPlan(unittest.TestCase):
    def test_noop_only_plan_rejects_every_physical_change_and_state_import(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);rows=[{'address':'aws_kms_key.custody','type':'aws_kms_key','change':{'actions':['no-op']}}]
            def run(args,**kwargs):
                if args[:2]==['terraform','plan']:
                    Path(next(arg.removeprefix('-out=') for arg in args if arg.startswith('-out='))).write_bytes(b'unit private plan')
                    return b''
                self.assertEqual(args[:2],['terraform','show'])
                return json.dumps({'resource_changes':rows}).encode()
            with patch.object(bootstrap,'run',side_effect=run),patch.dict(os.environ,ENV|{'PILOT_CREATION_IDENTITY':'{}'},clear=True):
                bootstrap.plan(work/'configuration.json',work,custody_only=True,observation_only=True)
                for actions in [['create'],['update'],['delete'],['delete','create']]:
                    rows[0]['change']={'actions':actions}
                    with self.subTest(actions=actions),self.assertRaises(ValueError):
                        bootstrap.plan(work/'configuration.json',work,custody_only=True,observation_only=True)
                rows[0]['change']={'actions':['no-op'],'importing':{'id':'existing-unadmitted-resource'}}
                with self.assertRaises(ValueError):
                    bootstrap.plan(work/'configuration.json',work,custody_only=True,observation_only=True)


if __name__=='__main__':unittest.main()
