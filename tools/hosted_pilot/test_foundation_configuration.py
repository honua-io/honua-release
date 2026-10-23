"""Candidate-independent funding is compiled from exact reviewed source bytes."""
import copy
import hashlib
import json
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
        self.basis = (source_root/'certification/hosted-pilot/foundation-funding-basis.json').read_bytes()
        self.sha = 'a'*40

    def resolve(self):
        def git(argv, **kwargs):
            self.assertEqual(argv[:2], ['git','show'])
            self.assertTrue(argv[2].startswith(self.sha+':'))
            return self.raw if argv[2] == self.sha+':'+self.relative else self.basis
        with patch('bootstrap.run', side_effect=git):
            return bootstrap.resolve_image_funding(self.cfg, self.root, self.sha)

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
        with self.assertRaisesRegex(ValueError, 'foundation identity'):
            self.resolve()

    def test_reduced_original_hold_cleanup_or_expanded_publication_allowance_refuse(self):
        for key,value in (('reserved_usd',0),('reserved_usd',233),('cleanup_reserve_usd',50),('deployment_reservation_usd',6)):
            before = copy.deepcopy(self.cfg)
            self.cfg['budget_ledger'][key] = value
            with self.subTest(key=key,value=value), self.assertRaisesRegex(ValueError, 'cannot discard original nonzero'):
                self.resolve()
            self.cfg = before

    def test_existing_explicit_source_budget_is_not_rebound(self):
        self.cfg.pop('image_preparation_funding')
        self.cfg['budget_ledger'].update(source_sha='b'*40,config_sha256='c'*64)
        with patch('bootstrap.run') as process:
            self.assertEqual(bootstrap.resolve_image_funding(self.cfg,self.root,self.sha),self.cfg)
        process.assert_not_called()
