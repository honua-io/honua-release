"""Foundation receipts bind actual STS identity and allowlisted plan resources."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import bootstrap


class BootstrapExecutionIdentity(unittest.TestCase):
    def setUp(self):
        self.identity={'foundation_source_sha':'a'*40,'release_root_sha':'b'*40,'config_path':'reviewed.json',
            'config_sha256':'c'*64,'provider_lock_sha256':'d'*64}
        self.caller={'Account':'585192672263','Arn':'arn:aws:iam::585192672263:user/pipeline',
            'UserId':'AIDAEXAMPLE123','AccessKeyId':'private-sentinel','credentials':{'secret':'private-sentinel'}}
        self.tags=bootstrap.creation_tags(self.identity,'42')
        self.resource={'address':'aws_iam_role.backend','type':'aws_iam_role',
            'change':{'actions':['create'],'after':{'name':'honua-release-pilot-foss4g-foundation-backend',
                'tags_all':self.tags,'password':'private-sentinel','assume_role_policy':'private-sentinel'}}}
    def test_sts_projection_keeps_actual_public_principal_and_static_limitation_only(self):
        value=bootstrap.execution_identity(self.caller,self.identity,'585192672263','42','1')
        self.assertEqual(value['principal_arn'],self.caller['Arn'])
        self.assertEqual(value['principal_id'],self.caller['UserId'])
        self.assertFalse(value['run_unique_session'])
        self.assertNotIn('private-sentinel',json.dumps(value))
    def test_foreign_account_malformed_principal_root_and_arn_lookalike_refuse(self):
        for change in ({'Account':'999999999999'},{'Arn':'arn:aws:iam::585192672263:root'},
            {'Arn':'arn:aws:iam::585192672263:assumed-role/pipeline/run'},
            {'UserId':'private-sentinel'},{'Arn':'not-a-principal'}):
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'actual supported STS'):
                bootstrap.execution_identity(self.caller|change,self.identity,'585192672263','42','1')
    def test_projection_contains_only_exact_named_created_resources_and_original_tags(self):
        unrelated={'address':'aws_secretsmanager_secret_version.private','type':'aws_secretsmanager_secret_version',
            'change':{'actions':['create'],'after':{'secret_string':'private-sentinel'}}}
        output=bootstrap.planned_creation_resources({'resource_changes':[self.resource,unrelated]})
        self.assertEqual(output,[{'address':self.resource['address'],'type':'aws_iam_role',
            'name':self.resource['change']['after']['name'],'creation_tags':self.tags}])
        self.assertNotIn('private-sentinel',json.dumps(output))
    def test_unknown_physical_name_or_missing_original_creation_tags_refuse(self):
        for change in ('name','execution','source'):
            row=copy.deepcopy(self.resource)
            if change=='name':row['change']['after'].pop('name')
            else:row['change']['after']['tags_all'].pop('BootstrapExecution' if change=='execution' else 'BootstrapReleaseSha')
            with self.subTest(change=change),self.assertRaises(ValueError):bootstrap.planned_creation_resources({'resource_changes':[row]})
    def test_actual_plan_preserves_custody_scope_across_second_phase_and_refuses_rebinding(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp);cfg=work/'cfg.json';cfg.write_text('{}')
            value={'resource_changes':[self.resource]}
            def run(argv,**kwargs):
                if argv[1]=='plan':
                    Path(next(x[5:] for x in argv if x.startswith('-out='))).write_bytes(b'original binary plan')
                    return b''
                return json.dumps(value).encode()
            with patch('bootstrap.run',side_effect=run),patch.dict('os.environ',{
                'GITHUB_RUN_ID':'42','PILOT_CREATION_IDENTITY':json.dumps(self.identity)}):
                bootstrap.plan(cfg,work)
                initial=json.loads((work/'creation-resources.json').read_text())
                value['resource_changes']=[]
                bootstrap.plan(cfg,work)
                self.assertEqual(json.loads((work/'creation-resources.json').read_text()),initial)
                row=copy.deepcopy(self.resource);row['change']['after']['name']='honua-release-pilot-foss4g-foundation-foreign'
                value['resource_changes']=[row]
                with self.assertRaisesRegex(ValueError,'identity changed'):bootstrap.plan(cfg,work)
