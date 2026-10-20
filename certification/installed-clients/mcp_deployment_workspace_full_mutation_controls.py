"""Pending integration controls; explicit offline originals, no candidate credit."""
import base64
import copy
import json
import unittest
from datetime import datetime
from mcp_deployment_workspace_full_controls import (
    full_controls,rehash_member,FIXTURE,bootstrap,full,semantic,replace,bind,guid,BASE,
)


class FullIdentityMutationControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.golden=full_controls()

    def judge(self,vector):
        return full.judge(FIXTURE,bootstrap(),vector['members']['sdk'],vector['terminal'])

    def test_coherent_wrong_studio_owner_passes_semantic_component_but_not_identity_join(self):
        vector=copy.deepcopy(self.golden)
        def mutate(envelope):
            value=envelope['observations'];primary=value['semantic']
            receipt=value['identityPreparation']['keys']['sdk']['primaryReceipt']
            old=receipt['identity']['studioActorId'];wrong=guid('wrong-but-coherent-studio-owner')
            for slot in ('storeBefore','storeAfter'):
                primary[slot]['stores']=replace(primary[slot]['stores'],{old:wrong})
            for index,call in enumerate(primary['calls']):
                if call['id'] not in ('deployment','restarted-deployment'):continue
                for content in call['result']['contents']:
                    content['text']=json.dumps(replace(json.loads(content['text']),{old:wrong}))
                seconds=(datetime.fromisoformat(call['startedAt'])-BASE).total_seconds()
                call.update(bind(call,'sdk',receipt['identity'],call['wire'].get('expectedTenantId'),
                    'coherent-wrong-owner:'+str(index),seconds))
            value['calls']=[*value['authority']['callsBefore'],*primary['calls'],*value['authority']['callsAfter']]
        rehash_member(vector,'sdk',mutate)
        primary=vector['members']['sdk']['semantic']
        self.assertIs(semantic.judge(FIXTURE,bootstrap(),primary,tenant_id='mcp-deployment-'+primary['nonce'])['semanticOnly'],True)
        with self.assertRaisesRegex(ValueError,'identity|owner|managed'):
            self.judge(vector)

    def test_rehashed_client_lifetime_overlap_refused(self):
        vector=copy.deepcopy(self.golden)
        rehash_member(vector,'inspector',lambda envelope:envelope.update(startedAt=vector['terminal']['roleReceipt']['cohort']['journeys'][0]['startedAt']))
        with self.assertRaisesRegex(ValueError,'lifetimes overlap'):
            self.judge(vector)

    def test_rehashed_between_client_runtime_reset_refused(self):
        vector=copy.deepcopy(self.golden)
        def mutate(envelope):
            value=envelope['observations']
            for group in (value['semantic'],value['authority']):
                group['runtimeBefore']['startedAt']='2026-10-20T00:14:00+00:00'
            value['authority']['limitedRead']['runtimeBefore']['startedAt']='2026-10-20T00:14:00+00:00'
            value['authority']['limitedRead']['runtimeAfter']['startedAt']='2026-10-20T00:14:00+00:00'
        rehash_member(vector,'inspector',mutate)
        with self.assertRaisesRegex(ValueError,'restart lineage'):
            self.judge(vector)


if __name__=='__main__':unittest.main()
