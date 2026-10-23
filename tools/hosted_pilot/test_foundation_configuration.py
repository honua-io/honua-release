"""Candidate-independent funding is compiled from exact reviewed source bytes."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import bootstrap


class FoundationConfiguration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.relative = 'certification/hosted-pilot/deployments/foss4g-image-preparation.tfvars.json'
        self.path = self.root/self.relative
        self.path.parent.mkdir(parents=True)
        source_root = Path(__file__).resolve().parents[2]
        self.raw = (source_root/self.relative).read_bytes()
        self.path.write_bytes(self.raw)
        self.cfg = json.loads((source_root/'certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json').read_text())
        self.reviewed_cfg = copy.deepcopy(self.cfg)
        self.basis = (source_root/'certification/hosted-pilot/foundation-funding-basis.json').read_bytes()
        self.sha = 'a'*40

    def resolve(self):
        def git(argv, **kwargs):
            self.assertEqual(argv[:2], ['git','show'])
            self.assertTrue(argv[2].startswith(self.sha+':'))
            if argv[2] == self.sha+':'+self.relative:
                return self.raw
            if argv[2] == self.sha+':certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json':
                return json.dumps(self.reviewed_cfg).encode()
            return self.basis
        with patch('bootstrap.run', side_effect=git):
            return bootstrap.resolve_image_funding(self.cfg, self.root, self.sha,
                config_path='certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json')

    def test_actual_source_and_config_identity_replace_only_explicit_unresolved_fields(self):
        before = copy.deepcopy(self.cfg)
        resolved = self.resolve()
        self.assertEqual(self.cfg, before)
        self.assertNotIn('image_preparation_funding', resolved)
        self.assertEqual(resolved['budget_ledger']['source_sha'], self.sha)
        self.assertEqual(resolved['budget_ledger']['config_sha256'], hashlib.sha256(self.raw).hexdigest())
        for key in ('spent_usd','reserved_usd','deployment_reservation_usd','cleanup_reserve_usd','total_budget_usd'):
            self.assertEqual(resolved['budget_ledger'][key], before['budget_ledger'][key])
        self.assertGreaterEqual(resolved['budget_ledger']['reserved_usd'], 234)

    def test_partial_or_prefilled_binding_cannot_be_replaced(self):
        for field in ('source_sha','config_sha256'):
            before = copy.deepcopy(self.cfg)
            self.cfg['budget_ledger'][field] = 'b'*64
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'cannot replace an existing'):
                self.resolve()
            self.cfg = before

    def test_changed_untracked_bytes_or_another_foundation_identity_refuse(self):
        self.path.write_bytes(self.raw+b' ')
        with self.assertRaisesRegex(ValueError, 'exact workflow source'):
            self.resolve()
        self.path.write_bytes(self.raw)
        self.cfg['name_prefix'] += '-foreign'
        with self.assertRaisesRegex(ValueError, 'complete bootstrap input differs'):
            self.resolve()

    def test_reduced_original_hold_cleanup_or_expanded_publication_allowance_refuse(self):
        for key,value in (('reserved_usd',0),('reserved_usd',233),('cleanup_reserve_usd',50),('deployment_reservation_usd',6)):
            before = copy.deepcopy(self.cfg)
            self.cfg['budget_ledger'][key] = value
            self.reviewed_cfg = copy.deepcopy(self.cfg)
            with self.subTest(key=key,value=value), self.assertRaisesRegex(ValueError, 'cannot discard original nonzero'):
                self.resolve()
            self.cfg = before
            self.reviewed_cfg = copy.deepcopy(before)

    def test_every_copied_top_level_and_budget_field_must_match_reviewed_source(self):
        original = copy.deepcopy(self.cfg)
        for field in original:
            if field in ('image_preparation_funding', 'budget_ledger'):
                continue
            self.cfg = copy.deepcopy(original)
            value = self.cfg[field]
            self.cfg[field] = not value if isinstance(value, bool) else value+['unreviewed'] if isinstance(value, list) else value+'-unreviewed'
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'complete bootstrap input differs'):
                self.resolve()
        for field in original['budget_ledger']:
            if field in ('source_sha', 'config_sha256'):
                continue  # Independently refused before source lookup above.
            self.cfg = copy.deepcopy(original)
            value = self.cfg['budget_ledger'][field]
            self.cfg['budget_ledger'][field] = value+1 if isinstance(value, (int, float)) else value+'-unreviewed'
            with self.subTest(budgetField=field), self.assertRaisesRegex(ValueError, 'complete bootstrap input differs'):
                self.resolve()
        self.cfg = copy.deepcopy(original)
        self.cfg['unexpected_input'] = 'unreviewed'
        with self.assertRaisesRegex(ValueError, 'complete bootstrap input differs'):
            self.resolve()

    def test_compiler_flag_and_json_boolean_numeric_substitution_cannot_change_identity(self):
        original = copy.deepcopy(self.cfg)
        for flag in (False, 1, 'true'):
            self.cfg = copy.deepcopy(original)
            self.cfg['image_preparation_funding'] = flag
            with self.subTest(flag=flag), self.assertRaisesRegex(ValueError, 'reviewed funding compiler mode'):
                self.resolve()
        for value in (False, 0.0):
            self.cfg = copy.deepcopy(original)
            self.cfg['budget_ledger']['spent_usd'] = value
            with self.subTest(spent=value), self.assertRaisesRegex(ValueError, 'complete bootstrap input differs'):
                self.resolve()

    def test_existing_explicit_source_budget_is_not_rebound(self):
        self.cfg.pop('image_preparation_funding')
        self.cfg['budget_ledger'].update(source_sha='b'*40,config_sha256='c'*64)
        with patch('bootstrap.run') as process:
            self.assertEqual(bootstrap.resolve_image_funding(self.cfg,self.root,self.sha,
                config_path='certification/hosted-pilot/bootstrap/deployments/other-reviewed.tfvars.json'),self.cfg)
        process.assert_not_called()

    def test_main_real_fixed_path_cannot_remove_mode_and_prefill_hashes_before_aws_or_plan(self):
        foundation = self.root/'iac'
        relative='certification/hosted-pilot/bootstrap/deployments/foss4g-images.tfvars.json'
        path=foundation/relative;path.parent.mkdir(parents=True)
        changed=copy.deepcopy(self.cfg);changed.pop('image_preparation_funding')
        changed['budget_ledger'].update(source_sha='b'*40,config_sha256='c'*64)
        path.write_text(json.dumps(changed))
        env={'GITHUB_REPOSITORY':'honua-io/honua-iac','GITHUB_REF':'refs/heads/trunk','GITHUB_SHA':'d'*40,
            'PILOT_RELEASE_ROOT_SHA':self.sha,'PILOT_FOUNDATION_SOURCE_ROOT':str(foundation),'RUNNER_TEMP':str(self.root)}
        def git(argv,**kwargs):
            if argv[:2]==['git','rev-parse']:return self.sha.encode()
            if argv[:4]==['git','-C',str(foundation),'rev-parse']:return ('d'*40).encode()
            if argv[:4]==['git','-C',str(foundation),'ls-files']:return b''
            raise AssertionError('unreviewed command before input admission')
        for selected in (relative, relative.replace('/foss4g-images','/./foss4g-images')):
            with self.subTest(selected=selected),patch.dict(os.environ,env,clear=True),patch('sys.argv',['bootstrap.py','plan','--config',selected]),\
                    patch('bootstrap.run',side_effect=git),patch('bootstrap.aws',side_effect=AssertionError('AWS before admission')) as aws,\
                    patch('bootstrap.plan') as plan,patch('bootstrap.private_json') as private:
                with self.assertRaisesRegex(ValueError,'fixed image input requires'):
                    bootstrap.main()
                aws.assert_not_called();plan.assert_not_called();private.assert_not_called()
