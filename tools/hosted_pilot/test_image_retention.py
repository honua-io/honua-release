"""Abandoned immutable pushes cannot bypass finite repository expiry."""
import copy
import json
import unittest
from image_retention import policy, verify

class ImageRetention(unittest.TestCase):
    def response(self, value):
        return json.dumps({'registryId':'585192672263','repositoryName':'honua-pilot-unit-worker','lifecyclePolicyText':json.dumps(value)})
    def check(self, value):
        calls=[]
        def read(argv, **kwargs):calls.append(argv);return self.response(value)
        result=verify('honua-pilot-unit-worker','585192672263','us-east-1',call=read)
        self.assertEqual(calls[0][1:3], ['ecr','get-lifecycle-policy'])
        return result
    def test_actual_policy_bounds_tagged_retries_and_untagged_layers(self):
        result=self.check(policy())
        self.assertEqual(result['expiryEligibilityDays'],14)
        self.assertEqual(result['untaggedExpiryEligibilityDays'],1)
        self.assertEqual(result['source'],'ecr:GetLifecyclePolicy')
    def test_missing_indefinite_tag_only_and_changed_scope_refuse_publication(self):
        for change in ('missing','longer','tagged-only','untagged-only','wrong-action'):
            value=copy.deepcopy(policy())
            if change=='missing':value={}
            elif change=='longer':value['rules'][1]['selection']['countNumber']=9999
            elif change=='tagged-only':value['rules'][1]['selection'].update(tagStatus='tagged',tagPrefixList=['known-'])
            elif change=='untagged-only':value['rules'].pop()
            else:value['rules'][1]['action']['type']='keep'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'finite immutable'):self.check(value)
    def test_another_registry_or_repository_cannot_supply_retention_proof(self):
        for field in ('registryId','repositoryName'):
            value=json.loads(self.response(policy()));value[field]='foreign'
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'actual worker registry'):
                verify('honua-pilot-unit-worker','585192672263','us-east-1',call=lambda *args,**kwargs:json.dumps(value))
    def test_controller_requires_its_own_exact_policy_on_actual_registry_response(self):
        name = 'honua-pilot-unit-trusted-controller'
        value = {'registryId':'585192672263','repositoryName':name,'lifecyclePolicyText':json.dumps(policy('controller'))}
        observed = verify(name,'585192672263','us-east-1',kind='controller',call=lambda *args,**kwargs:json.dumps(value))
        self.assertEqual(observed['expiryEligibilityDays'],14)
        value['lifecyclePolicyText'] = json.dumps(policy('worker'))
        with self.assertRaisesRegex(ValueError,'actual controller registry'):
            verify(name,'585192672263','us-east-1',kind='controller',call=lambda *args,**kwargs:json.dumps(value))
