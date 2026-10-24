"""Full native claim/schedule policies reject broadened or delayed controls."""
import copy
from datetime import datetime, timezone
import json
import unittest
from worker_readiness import verify_claim_policy, verify_schedule

class Claims(unittest.TestCase):
    def setUp(self):
        self.admin = 'arn:aws:iam::585192672263:role/honua-pilot-unit-plan'
        self.worker = 'arn:aws:iam::585192672263:role/honua-agent-unit-attempt-lifecycle'
        self.table = 'arn:aws:dynamodb:us-east-1:585192672263:table/honua-agent-unit-claims'
        self.roles = {'administrators':[self.admin], 'workers':[self.worker]}
        self.policy = {'Version':'2012-10-17','Statement':[
            {'Sid':'RuntimeCanOnlyClaimOrObserve','Effect':'Deny','Principal':'*','NotAction':['dynamodb:PutItem','dynamodb:GetItem','dynamodb:DescribeTable'],
             'Resource':self.table,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':[self.admin]}}},
            {'Sid':'OnlyFixedLifecycleRolesMayClaim','Effect':'Deny','Principal':'*','Action':'dynamodb:PutItem',
             'Resource':self.table,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':sorted([self.admin,self.worker])}}},
            {'Sid':'OnlyFixedLifecycleMayTransactWrites','Effect':'Deny','Principal':'*','Action':'dynamodb:*',
             'Resource':self.table,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':sorted([self.admin,self.worker])},'StringEquals':{'dynamodb:EnclosingOperation':'TransactWriteItems'}}},
            {'Sid':'RuntimeCannotTransactReads','Effect':'Deny','Principal':'*','Action':'dynamodb:*',
             'Resource':self.table,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':[self.admin]},'StringEquals':{'dynamodb:EnclosingOperation':'TransactGetItems'}}},
            {'Sid':'RuntimeClaimWritesMustBeTransactional','Effect':'Deny','Principal':'*','Action':'dynamodb:PutItem',
             'Resource':self.table,'Condition':{'ArnNotEquals':{'aws:PrincipalArn':[self.admin]},'StringNotEquals':{'dynamodb:EnclosingOperation':'TransactWriteItems'}}}]}
    def check(self):
        verify_claim_policy(self.policy, table_arn=self.table, roles=self.roles, required=[self.worker],account_id='585192672263')
    def test_exact_complete_source_policy_accepts_reordered_statements(self):
        self.check();self.policy['Statement'].reverse();self.check()
    def test_same_sid_cannot_hide_any_broadened_field_or_writer(self):
        original=copy.deepcopy(self.policy)
        for change in ('effect','principal','action','resource','extra-exemption','extra-allow','missing-sibling','duplicate-sid','changed-transaction-condition'):
            self.policy=copy.deepcopy(original)
            row=self.policy['Statement'][1]
            if change=='effect':row['Effect']='Allow'
            elif change=='principal':row['Principal']={'AWS':self.worker}
            elif change=='action':row['Action']='dynamodb:GetItem'
            elif change=='resource':row['Resource']='*'
            elif change=='extra-exemption':row['Condition']['ArnNotEquals']['aws:PrincipalArn'].append('arn:aws:iam::585192672263:role/unreviewed')
            elif change=='extra-allow':self.policy['Statement'].append({'Sid':'Unreviewed','Effect':'Allow','Principal':'*','Action':'dynamodb:*','Resource':self.table})
            elif change=='missing-sibling':self.policy['Statement'].pop()
            elif change=='duplicate-sid':self.policy['Statement'].append(copy.deepcopy(row))
            else:self.policy['Statement'][2]['Condition']['StringEquals']['dynamodb:EnclosingOperation']='TransactGetItems'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'complete actual claim-table'):self.check()
    def test_current_worker_does_not_authorize_other_unreviewed_workers(self):
        for roles in ({'administrators':[self.admin], 'workers':['arn:aws:iam::585192672263:role/honua-agent-foreign-attempt-lifecycle']},
                      {'administrators':[self.worker], 'workers':[self.worker]}):
            self.roles=roles
            with self.subTest(roles=roles),self.assertRaisesRegex(ValueError,'complete reviewed'):self.check()

class CleanupSchedule(unittest.TestCase):
    def setUp(self):
        self.expiry=2000000000
        self.arn='arn:aws:scheduler:us-east-1:585192672263:schedule/unit/expiry'
        self.cleanup={'state_machine_arn':'arn:aws:states:us-east-1:585192672263:stateMachine:unit-reaper','dead_letter_arn':'arn:aws:sqs:us-east-1:585192672263:unit-dead'}
        self.expected={'schedule_expression':'rate(1 minute)','schedule_expression_timezone':'UTC','start_date':datetime.fromtimestamp(self.expiry,timezone.utc).isoformat(),
            'end_date':datetime.fromtimestamp(self.expiry+86400,timezone.utc).isoformat(),'state':'ENABLED','action_after_completion':'NONE',
            'flexible_time_window':[{'mode':'OFF'}],'target':[{'arn':self.cleanup['state_machine_arn'],'role_arn':'arn:aws:iam::585192672263:role/unit-schedule','input':'{}',
                'dead_letter_config':[{'arn':self.cleanup['dead_letter_arn']}],'retry_policy':[{'maximum_event_age_in_seconds':300,'maximum_retry_attempts':2}]}]}
        self.actual={'Arn':self.arn,'State':'ENABLED','ScheduleExpression':'rate(1 minute)','ScheduleExpressionTimezone':'UTC',
            'StartDate':self.expected['start_date'],'EndDate':self.expected['end_date'],'ActionAfterCompletion':'NONE','FlexibleTimeWindow':{'Mode':'OFF'},
            'Target':{'Arn':self.cleanup['state_machine_arn'],'RoleArn':self.expected['target'][0]['role_arn'],'Input':'{}',
                'DeadLetterConfig':{'Arn':self.cleanup['dead_letter_arn']},'RetryPolicy':{'MaximumEventAgeInSeconds':300,'MaximumRetryAttempts':2}}}
    def check(self):verify_schedule(self.actual,self.expected,schedule_arn=self.arn,cleanup=self.cleanup,expires_epoch=self.expiry)
    def test_exact_native_schedule_matches_original_absolute_expiry(self):self.check()
    def test_delayed_unbounded_wrong_attempt_or_role_cannot_claim_cleanup_verified(self):
        original=copy.deepcopy(self.actual)
        for change in ('expression','timezone','start','end','window','role','input','retry','dlq','completion'):
            self.actual=copy.deepcopy(original)
            if change=='expression':self.actual['ScheduleExpression']='rate(1 day)'
            elif change=='timezone':self.actual['ScheduleExpressionTimezone']='Pacific/Honolulu'
            elif change=='start':self.actual['StartDate']=datetime.fromtimestamp(self.expiry+60,timezone.utc).isoformat()
            elif change=='end':self.actual['EndDate']=datetime.fromtimestamp(self.expiry+86401,timezone.utc).isoformat()
            elif change=='window':self.actual['FlexibleTimeWindow']={'Mode':'FLEXIBLE','MaximumWindowInMinutes':60}
            elif change=='role':self.actual['Target']['RoleArn']='foreign-role'
            elif change=='input':self.actual['Target']['Input']='{"attemptId":"foreign"}'
            elif change=='retry':self.actual['Target']['RetryPolicy']['MaximumEventAgeInSeconds']=86400
            elif change=='dlq':self.actual['Target']['DeadLetterConfig']['Arn']='foreign-dlq'
            else:self.actual['ActionAfterCompletion']='DELETE'
            with self.subTest(change=change),self.assertRaisesRegex(ValueError,'actual independent expiry'):self.check()
