"""Original run/state identity controls; these tests do not delete infrastructure."""
import copy
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock
from botocore.exceptions import ClientError

from retire_cloud_cell import RecoveryRefused, compute_absence, original_request, verify_handoff


class RecoveryBindingTests(unittest.TestCase):
    def setUp(self):
        self.metadata = {"id": 39911551529, "run_attempt": 1, "repository":{"full_name":"honua-io/honua-release"},
                         "path":".github/workflows/e2e-cloud-aws.yml", "head_branch":"trunk", "event":"workflow_dispatch",
                         "status":"completed", "head_sha":"a"*40}
        self.manifest = (b'components:\n  honua-iac:\n    repository: https://github.com/honua-io/honua-iac\n'
                         b'    sha: "' + b'b'*40 + b'"\n')

    def request(self):
        return original_request(self.metadata, self.manifest, run_id="39911551529", attempt="1", redis="off")

    def test_original_completed_run_and_exact_manifest_are_bound(self):
        request = self.request()
        verify_handoff(request, {**request, "target":"aws-serverless", "provisionAttempted":True,
                                 "startedAt":"2026-10-23T06:28:53Z"})
        self.assertEqual(request["artifactSuffix"], "aws-serverless-redis-off-1")

    def test_running_fork_wrong_attempt_and_wrong_workflow_refuse(self):
        for patch in ({"status":"in_progress"}, {"head_branch":"feature"}, {"run_attempt":2},
                      {"repository":{"full_name":"someone/fork"}}, {"path":".github/workflows/arbitrary.yml"},
                      {"event":"pull_request"}, {"head_sha":"trunk"}):
            saved = copy.deepcopy(self.metadata)
            self.metadata.update(patch)
            with self.subTest(patch=patch), self.assertRaises(RecoveryRefused):
                self.request()
            self.metadata = saved

    def test_stale_candidate_or_foreign_cell_cannot_authorize_cleanup(self):
        request = self.request()
        handoff = {**request, "target":"aws-serverless", "provisionAttempted":True, "startedAt":"2026-10-23T06:28:53Z"}
        for key in ("runId", "runAttempt", "cell", "candidateDigest"):
            with self.subTest(key=key), self.assertRaises(RecoveryRefused):
                verify_handoff(request, {**handoff,key:"different"})

    def test_native_absence_keeps_pending_secret_recovery_explicit(self):
        session = Mock()
        session.client.return_value.get_function.side_effect = ClientError(
            {'Error': {'Code': 'ResourceNotFoundException'}}, 'GetFunction')
        session.client.return_value.describe_secret.return_value = {'DeletedDate':datetime(2026,10,23,tzinfo=timezone.utc)}
        observed = compute_absence({'resources':[
            {'type':'aws_lambda_function','id':'original','address':'module.honua.aws_lambda_function.this'},
            {'type':'aws_secretsmanager_secret','id':'original-secret','address':'module.honua.aws_secretsmanager_secret.runtime_settings'}]},session)
        self.assertEqual(len(observed['resources']),1)
        self.assertTrue(observed['resources'][0]['absent'])
        self.assertEqual(observed['pendingSecretDeletion'][0]['arn'],'original-secret')

    def test_unreadable_or_still_present_compute_cannot_be_called_absent(self):
        record={'resources':[{'type':'aws_lambda_function','id':'original','address':'owned'}]}
        session=Mock()
        session.client.return_value.get_function.side_effect=ClientError({'Error':{'Code':'AccessDeniedException'}},'GetFunction')
        with self.assertRaises(ClientError):
            compute_absence(record,session)
        session.client.return_value.get_function.side_effect=None
        session.client.return_value.get_function.return_value={'Configuration':{'FunctionName':'original'}}
        with self.assertRaises(RecoveryRefused):
            compute_absence(record,session)

    def test_active_secret_cannot_be_reported_as_pending_deletion(self):
        session=Mock()
        session.client.return_value.describe_secret.return_value={'Name':'original-secret'}
        with self.assertRaises(RecoveryRefused):
            compute_absence({'resources':[{'type':'aws_secretsmanager_secret','id':'original-secret','address':'owned'}]},session)


if __name__ == "__main__":
    unittest.main()
