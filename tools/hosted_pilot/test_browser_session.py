"""Private capture projects actual identity only; machine sessions fail closed."""
import copy
from datetime import datetime, timedelta, timezone
import json
import unittest
from unittest.mock import MagicMock, patch
import browser_session as session

class BrowserSession(unittest.TestCase):
    def setUp(self):
        self.deployment = {'region': 'us-east-1', 'clusterArn': 'arn:aws:ecs:us-east-1:585192672263:cluster/honua-pilot-unit',
            'serviceArn': 'arn:aws:ecs:us-east-1:585192672263:service/honua-pilot-unit/honua-pilot-unit-console',
            'taskDefinitionArn': 'arn:aws:ecs:us-east-1:585192672263:task-definition/honua-pilot-unit-console:7',
            'imageUri': 'ghcr.io/honua-io/honua-console@sha256:'+'a'*64,
            'edgeImageUri': 'quay.io/oauth2-proxy/oauth2-proxy@sha256:'+'b'*64,
            'issuer': 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Unit'}
        headers = [{'name': key, 'preserveRequestValue': False, 'values': []} for key in ('Authorization', 'X-API-Key', 'X-Forwarded-Access-Token')]
        headers += [{'name': 'X-Forwarded-User', 'preserveRequestValue': False, 'values': [{'claimSource': {'claim': 'user'}}]},
            {'name': 'X-Honua-Edge-Auth', 'preserveRequestValue': False, 'values': [{'secretSource': {'fromEnv': 'HONUA_EDGE_SHARED_SECRET'}}]}]
        self.edge = {'providers': [{'provider': 'oidc', 'clientID': 'actual-client', 'oidcConfig': {'issuerURL': self.deployment['issuer'],
            'userIDClaim': 'sub', 'insecureSkipNonce': False, 'insecureSkipIssuerVerification': False, 'insecureAllowUnverifiedEmail': False}}],
            'injectRequestHeaders': headers}
        shared = 'arn:aws:secretsmanager:us-east-1:585192672263:secret:honua-pilot-unit/edge/session-ABC123:shared::'
        self.task = {'taskDefinitionArn': self.deployment['taskDefinitionArn'], 'containerDefinitions': [
            {'name': 'console', 'image': self.deployment['imageUri'], 'environment': [{'name': key, 'value': value} for key,value in {
                'ASPNETCORE_ENVIRONMENT':'Production', 'ASPNETCORE_URLS':'http://127.0.0.1:8081', 'Honua__Console__Auth__Mode':'EdgeForwarded',
                'Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer':self.deployment['issuer']}.items()],
             'secrets': [{'name':'Honua__Console__Auth__EdgeForwarded__SharedSecret','valueFrom':shared}]},
            {'name': 'edge', 'image': self.deployment['edgeImageUri'], 'environment': [], 'secrets': [{'name':'HONUA_EDGE_SHARED_SECRET','valueFrom':shared}]}]}
        self.service = {'serviceArn':self.deployment['serviceArn'], 'clusterArn':self.deployment['clusterArn'],
            'taskDefinition':self.deployment['taskDefinitionArn'], 'status':'ACTIVE', 'desiredCount':1, 'runningCount':1, 'pendingCount':0,
            'deployments':[{'taskDefinition':self.deployment['taskDefinitionArn'],'status':'PRIMARY','rolloutState':'COMPLETED'}]}
        self.ecs = MagicMock()
        self.ecs.describe_services.return_value = {'services':[self.service]}
        self.ecs.describe_task_definition.return_value = {'taskDefinition':self.task}
        self.profile = {'id':'actual-profile','serverUri':'https://agent.pilot.demo.honua.io/'}
        self.subject = 'actual-validated-human-subject'
        self.partition = session.operator_partition(self.subject,self.profile)
        self.public_session = {'authenticated':True,'principalId':self.partition,'environmentId':'actual-profile',
            'expiresAt':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat(), 'csrf':'private-csrf-sentinel'}
        self.target = {'targetId':'actual-native-target','principal':{'actorId':'actual-opaque-human','studioOwnerId':'opaque-native-owner','subjectId':self.subject,
            'authenticationKind':'operator-bearer','isHumanSession':True}}
        self.config = {'endpoint':'https://pilot.demo.honua.io','targetId':'actual-native-target','consoleDeployment':self.deployment,'consoleProfile':self.profile}
    def observe(self):
        self.task['containerDefinitions'][1]['environment'] = [{'name':'HONUA_EDGE_CONFIG','value':json.dumps(self.edge)},
            {'name':'OAUTH2_PROXY_COOKIE_SECURE','value':'true'},{'name':'OAUTH2_PROXY_COOKIE_HTTPONLY','value':'true'}]
        def read(path, binding=None):
            if path == session.SESSION_PATH: return self.public_session
            self.assertEqual(path, session.TARGET_PATH)
            self.assertEqual(binding, {'principalId':self.public_session['principalId'],'environmentId':self.public_session['environmentId']})
            return self.target
        return session.observe(self.config, {}, ecs=self.ecs, read=read)
    def test_retains_only_observed_console_partition_environment_and_native_human_pair(self):
        actual = self.observe()
        self.assertEqual(actual['nativeTarget'],self.target)
        self.assertEqual(actual['session'],{'principalId':self.partition,'environmentId':'actual-profile','authenticated':True})
        self.assertNotIn('csrf',json.dumps(actual)); self.assertNotIn('private-csrf-sentinel',json.dumps(actual))
    def test_api_key_and_absent_native_projection_cannot_become_human(self):
        original = copy.deepcopy(self.target)
        for principal in ({}, {'actorId':'apikey:api-key:uuid','authenticationKind':'api-key','isHumanSession':False},
                          {'actorId':'caller-human-flag','authenticationKind':'api-key','isHumanSession':True}):
            self.target = original | {'principal':principal}
            with self.subTest(principal=principal),self.assertRaisesRegex(ValueError,'server-observed actual human'): self.observe()
    def test_development_stale_task_issuer_bypass_and_forwarded_header_override_refuse(self):
        original = copy.deepcopy((self.task,self.service,self.edge))
        for change in ('development','stale-service','wrong-issuer','oidc-bypass','forward-key','unbound-human-header'):
            self.task,self.service,self.edge = copy.deepcopy(original)
            self.ecs.describe_services.return_value = {'services':[self.service]}
            self.ecs.describe_task_definition.return_value = {'taskDefinition':self.task}
            if change == 'development': self.task['containerDefinitions'][0]['environment'][0]['value']='Development'
            elif change == 'stale-service': self.service['taskDefinition']='old-definition:1'
            elif change == 'wrong-issuer': self.edge['providers'][0]['oidcConfig']['issuerURL']='https://foreign.example'
            elif change == 'oidc-bypass': self.edge['providers'][0]['oidcConfig']['insecureSkipIssuerVerification']=True
            elif change == 'forward-key': self.edge['injectRequestHeaders'][1]['preserveRequestValue']=True
            else: self.edge['injectRequestHeaders'][-2]['values']=[{'claimSource':{'claim':'untrusted'}}]
            with self.subTest(change=change),self.assertRaises(ValueError): self.observe()
    def test_expired_or_different_target_refuses(self):
        self.public_session['expiresAt']='2020-01-01T00:00:00Z'
        with self.assertRaisesRegex(ValueError,'expired'): self.observe()
        self.public_session['expiresAt']=(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()
        self.target['targetId']='foreign-target'
        with self.assertRaisesRegex(ValueError,'selected target'): self.observe()
    def test_same_shape_lookalike_issuer_host_refuses_before_native_observation(self):
        original=self.deployment['issuer']
        for host in ('cognito-idpXus-east-1.amazonaws.com','cognito-idp.us-east-1Xamazonaws.com','cognito-idp.us-east-1.amazonawsXcom'):
            lookalike='https://'+host+'/us-east-1_Unit'
            self.deployment['issuer']=lookalike
            self.edge['providers'][0]['oidcConfig']['issuerURL']=lookalike
            env=self.task['containerDefinitions'][0]['environment']
            for entry in env:
                if entry['name']=='Honua__Console__Auth__EdgeForwarded__ServerSessionIssuer':entry['value']=lookalike
            self.ecs.reset_mock()
            with self.subTest(host=host),self.assertRaisesRegex(ValueError,'source-reviewed exact production'):
                self.observe()
            self.ecs.describe_services.assert_not_called()
        self.deployment['issuer']=original
    def test_partition_matches_genuine_dotnet_utf16_golden_vector(self):
        # Real compiled AgentGatewayBinding.OperatorPartition, assembly SHA256
        # f633bdaee233d69ee0ac8e83c9b4eb891d1112e2101878152253781c65b0eb2c.
        # The original C# reflection control observed UTF16 lengths 7 and 9.
        profile={'id':'profile𐐷','serverUri':'https://agent.pilot.demo.honua.io/'}
        self.assertEqual(session.operator_partition('human😀',profile),
            '08a8ee3ec381c19a3bfa73060340f2302a706a8b8c5b4f1a585bb44cbcdfd6f3')
        self.assertEqual(session.operator_partition('human😀',profile),
            session.operator_partition('human😀',profile|{'serverUri':profile['serverUri'].rstrip('/')}))
    def test_actual_subject_profile_or_origin_mismatch_cannot_join_console_partition(self):
        original=copy.deepcopy((self.target,self.profile,self.public_session))
        for change in ('missing-subject','foreign-subject','actor-as-subject','studio-owner-as-subject','profile','origin'):
            self.target,self.profile,self.public_session=copy.deepcopy(original)
            self.config['consoleProfile']=self.profile
            if change=='missing-subject':del self.target['principal']['subjectId']
            elif change=='foreign-subject':self.target['principal']['subjectId']='another-real-human'
            elif change=='actor-as-subject':self.target['principal']['subjectId']=self.target['principal']['actorId']
            elif change=='studio-owner-as-subject':self.target['principal']['subjectId']=self.target['principal']['studioOwnerId']
            elif change=='profile':self.profile['id']='foreign-profile'
            else:self.profile['serverUri']='https://api.pilot.demo.honua.io'
            with self.subTest(change=change),self.assertRaises(ValueError):self.observe()
    def test_browser_capture_cannot_reuse_operator_bearer_as_cookie_state(self):
        with self.assertRaises(ValueError): session.storage('an-actual-operator-bearer-is-not-storage')
        with self.assertRaisesRegex(ValueError,'isolated reviewed Console'): session.reader('https://foreign.example',{'cookies':[]})
