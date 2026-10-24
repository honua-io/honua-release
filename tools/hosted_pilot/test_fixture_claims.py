"""Later import outputs get distinct claims without replaying legacy effects."""
import copy
import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_e2e_pack import PackError, canonical_digest
from fixture_handoff import IMPORT_PROFILE, ownership_claim


class OwnershipClaims(unittest.TestCase):
    def setUp(self):
        self.context = {'campaignId': 'original-campaign', 'caseId': 'P04:A:aws-ecs-redis-on', 'attempt': 1, 'campaignMode': 'development'}
        self.base = canonical_digest({key: self.context[key] for key in ('campaignId', 'caseId', 'attempt')})
        self.request = {'candidateKey': 'sha256:'+'a'*64, 'profile': IMPORT_PROFILE,
                        'tables': [{'oid': 1001}], 'nativeImports': [{'jobId': 'actual-first-job'}]}
    def reference(self, request, *, legacy=False):
        digest = canonical_digest(request)
        return {'key': self.base if legacy else self.base+'/'+digest, 'sha256': digest}
    def test_distinct_later_output_has_its_own_immutable_claim(self):
        later = copy.deepcopy(self.request)
        later.update(tables=[{'oid': 1002}], nativeImports=[{'jobId': 'actual-later-job'}])
        first = ownership_claim(self.request, self.context, self.reference(self.request))
        second = ownership_claim(later, self.context, self.reference(later))
        self.assertNotEqual(first, second)
        self.assertEqual(first, ownership_claim(self.request, self.context, self.reference(self.request)))
    def test_legacy_import_claim_and_p10_claim_keep_original_identity(self):
        legacy = ownership_claim(self.request, self.context, self.reference(self.request, legacy=True))
        self.assertEqual(legacy, canonical_digest({'candidateKey': self.request['candidateKey'], 'context': self.context, 'kind': 'model-import-ownership'}))
        p10 = {key: value for key, value in self.request.items() if key != 'profile'}
        self.assertEqual(ownership_claim(p10, self.context, self.reference(p10, legacy=True)),
            canonical_digest({'candidateKey': p10['candidateKey'], 'context': self.context, 'kind': 'fixture-ownership'}))
        with self.assertRaisesRegex(PackError, 'exact original retained request'):
            ownership_claim(p10, self.context, self.reference(p10))
    def test_other_request_or_other_attempt_cannot_select_a_claim(self):
        selected = self.reference(self.request)
        for reference in [{**selected, 'sha256': 'sha256:'+'b'*64}, {**selected, 'key': self.base+'/sha256:'+'b'*64},
                          {**selected, 'key': 'sha256:'+'c'*64+'/'+canonical_digest(self.request)}]:
            with self.subTest(reference=reference), self.assertRaisesRegex(PackError, 'exact original retained request'):
                ownership_claim(self.request, self.context, reference)
