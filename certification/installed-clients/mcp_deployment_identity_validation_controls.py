"""Offline original-byte cohort joins; never primary or candidate evidence."""
import base64
import copy
from datetime import datetime,timezone,timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import mcp_deployment_identity_validation as subject
import mcp_deployment_identity_role as role
import mcp_managed_identity_fixture as managed
import mcp_managed_identity_fixture_controls as keys
from mcp_deployment_identity_configuration import declaration,digest,ENV
from mcp_deployment_identity_role_controls import OfflineRoleApi,mutate_journey
from mcp_render_validation_controls import runtime

NONCE='d'*12
SERVICE='owned_area'

def stamp():return datetime.now(timezone.utc).isoformat()

def complete_cohort():
    """Exercise real fixture/verifier code with explicitly controlled offline APIs."""
    selected=runtime();configuration=declaration('mcp-deployment-'+NONCE)
    observation={"schema":"honua.mcp-deployment-configuration-observation/v1","declarationSha256":digest(configuration),
        "containerId":selected['containerId'],"imageId":selected['imageId'],"environment":dict(ENV),"observedAt":stamp()}
    api=OfflineRoleApi()
    fixture=role.ManagedRoleFixture(admin_credential='controlled-private-admin',configuration=configuration,
        configuration_observation=observation,inspect_owned=runtime,
        observe=lambda identifier:{**api.observe(identifier),'startedAt':stamp()})
    state={"schema":"honua.mcp-deployment-managed-cohort/v1","nonce":NONCE,"serviceId":SERVICE,
        "candidateImage":selected['imageReference'],"candidate":{key:selected[key] for key in ('containerId','imageId','candidateSourceSha')},
        "credentialSalt":keys.SALT,"configuration":configuration,"configurationObservation":observation,
        "roleReceipt":fixture.receipt,"keys":{},"failures":[],"cleanupComplete":False}
    with tempfile.TemporaryDirectory() as directory,patch.object(role,'build_opener',return_value=api):
        fixture.create();pending=[]
        for selector in subject.CLIENTS:
            state['keys'][selector]={}
            for name,profile in [('primaryReceipt','admin'),('foreignReceipt','workspace-reader')]:
                index=len(pending)+1
                key_id=f'{index:08x}-1111-4111-8111-111111111111'
                secret='hnua_'+base64.urlsafe_b64encode(bytes([index])*32).decode().rstrip('=')
                normal=keys.OfflineNormalApi()
                identity=managed.ManagedIdentityFixture(admin_credential=keys.PRIVATE_ADMIN,
                    private_path=Path(directory)/f'{index}.private.json',receipt_path=Path(directory)/f'{index}.receipt.json',
                    inspect_owned=runtime,redis=normal.redis,credential_salt=keys.SALT,profile=profile,
                    **({'service_id':SERVICE} if profile=='workspace-reader' else {}))
                with patch.object(keys,'KEY_ID',key_id),patch.object(keys,'PRIVATE_KEY',secret),patch.object(managed,'build_opener',return_value=normal):identity.create()
                pending.append((identity,normal,key_id,secret));state['keys'][selector][name]=identity.receipt
        originals=[]
        for canonical in subject.CLIENTS.values():
            originals.append(json.dumps({"schema":"honua.mcp-deployment-client-journey/v1","canonicalClient":canonical,
                "cohortId":NONCE,"candidate":copy.deepcopy(fixture.receipt['candidate']),"configurationSha256":digest(configuration),
                "startedAt":stamp(),"completedAt":stamp(),"observations":{"canonicalClient":canonical,"nonce":NONCE,
                    "calls":[{"explicitOfflineCohortControl":True}]}}).encode())
        state['cleanupStartedAt']=stamp()
        for identity,normal,key_id,secret in pending:
            with patch.object(keys,'KEY_ID',key_id),patch.object(keys,'PRIVATE_KEY',secret),patch.object(managed,'build_opener',return_value=normal):identity.revoke()
        fixture.bind_journeys(originals);fixture.revoke();state['cleanupComplete']=True
    return state,selected

class OriginalCohortControls(unittest.TestCase):
    def judge(self,value,selected):return subject.verified_cohort(value,runtime=selected,nonce=NONCE,service_id=SERVICE)
    def test_positive_rejudges_all_four_normal_terminal_keys_and_both_original_client_members(self):
        state,selected=complete_cohort();result=self.judge(state,selected)
        self.assertEqual(set(result),set(subject.CLIENTS.values()))
        self.assertEqual(len({value['identity'][name]['keyId'] for value in result.values() for name in ('primaryReceipt','foreignReceipt')}),4)
        # This intake intentionally does not grant semantic/authority facets.
        self.assertNotIn('positive',result)
    def test_same_principal_alias_across_clients_refuses_despite_valid_individual_receipt(self):
        state,selected=complete_cohort()
        state['keys']['inspector']['primaryReceipt']=copy.deepcopy(state['keys']['sdk']['primaryReceipt'])
        with self.assertRaisesRegex(ValueError,'independently distinct original managed callers'):self.judge(state,selected)
    def test_missing_client_failed_cleanup_and_wrong_candidate_refuse(self):
        state,selected=complete_cohort()
        for mutate in (lambda value:value['keys'].pop('inspector'),lambda value:value.update(cleanupComplete=False),
                       lambda value:value['failures'].append({'phase':'retained-offline-failure'}),
                       lambda value:value['candidate'].update(containerId='0'*64)):
            changed=copy.deepcopy(state);mutate(changed)
            with self.assertRaisesRegex(ValueError,'complete original two-client managed cohort missing'):self.judge(changed,selected)
    def test_global_cleanup_must_follow_both_original_raw_members_even_after_coherent_rehash(self):
        state,selected=complete_cohort()
        cleanup=datetime.fromisoformat(state['cleanupStartedAt'])
        role_cleanup=datetime.fromisoformat(state['roleReceipt']['cleanupStartedAt'].replace('Z','+00:00'))
        self.assertGreater(role_cleanup,cleanup)
        within_role=(cleanup+(role_cleanup-cleanup)/2).isoformat()
        mutate_journey(state['roleReceipt']['cohort']['journeys'][0],lambda value:value.update(completedAt=within_role))
        # The role verifier accepts this genuine within-role bound; the stronger
        # parent rejects a journey after declared KEY cleanup began.
        role.verify_role_receipt(state['roleReceipt'],state['configuration'],state['configurationObservation'],selected)
        with self.assertRaisesRegex(ValueError,'cleanup must follow both complete original journeys'):self.judge(state,selected)
    def test_declared_cleanup_after_actual_key_revocation_refuses(self):
        state,selected=complete_cohort()
        state['cleanupStartedAt']=(datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'cleanup observations do not follow|revocation preceded declared shared cleanup'):self.judge(state,selected)
    def test_stale_independent_role_phase_cannot_substitute_for_live_cleanup(self):
        state,selected=complete_cohort()
        state['roleReceipt']['afterCleanupStore']['startedAt']=state['roleReceipt']['createdStore']['startedAt']
        # Each original DB payload remains internally valid and all four cluster
        # identities match; the complete parent must still reject stale timing.
        role.verify_role_receipt(state['roleReceipt'],state['configuration'],state['configurationObservation'],selected)
        with self.assertRaisesRegex(ValueError,'cleanup observations do not follow'):self.judge(state,selected)

if __name__=='__main__':unittest.main()
