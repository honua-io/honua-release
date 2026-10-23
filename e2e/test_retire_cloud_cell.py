"""Original run/state identity controls; these tests do not delete infrastructure."""
import copy
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from botocore.exceptions import ClientError

from retire_cloud_cell import RecoveryRefused, compute_absence, original_request, verify_handoff, perform


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

    def test_recovery_preserves_original_key_and_seals_private_diagnostics_on_success_or_failure(self):
        import run_cloud
        import cost_meter
        import targets.terraform_target
        for failed in (False, True):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                iac = base / 'iac'
                root = iac / 'infrastructure/terraform/examples/aws-serverless'
                root.mkdir(parents=True)
                output = base / 'output'
                output.mkdir()
                handoff = base / 'handoff'
                handoff.mkdir()
                request = self.request()
                (output / 'request.json').write_text(json.dumps(request))
                (output / 'original-platform-manifest.yaml').write_bytes(self.manifest)
                (handoff / 'provision.json').write_text(json.dumps({**request, 'target':'aws-serverless',
                    'provisionAttempted':True, 'startedAt':'2026-10-23T06:28:53Z'}))
                args = SimpleNamespace(directory=output, original_run=request['runId'], original_attempt='1',
                    redis='off', handoff=handoff, iac=iac, helper=base/'helper.py', bundle=base/'state.enc',
                    account_id='585192672263')
                target = Mock()
                target._iac_root.return_value = root
                target._read_transition_vars.return_value = ['-var=redis_enabled=false']
                session = Mock()
                session.client.return_value.get_caller_identity.return_value = {'Account':args.account_id}
                helper = Mock()
                helper.retire.return_value = {'terraformRetired':True,'destroyCount':126,'planSha256':'c'*64}
                if failed:
                    helper.retire.side_effect = RecoveryRefused('retained failure')
                state = {'resources':[
                    {'mode':'managed','type':'aws_lambda_function','name':'this','instances':[{'attributes':{
                        'image_uri':'candidate-lambda', 'architectures':['arm64'],
                        'tags_all':{'honua-release:cost-ceiling-usd':'20'}}}]},
                    {'mode':'managed','type':'aws_batch_job_definition','name':'gp','instances':[{'attributes':{
                        'container_properties':json.dumps({'image':'candidate-batch'})}}]}]}

                def open_original(*_):
                    (root / 'terraform.tfstate').write_text(json.dumps(state))

                def command(argv, **kwargs):
                    if argv[:2] == ['gh','api']:
                        return subprocess.CompletedProcess(argv,0,json.dumps(self.metadata),'')
                    if argv[0] == 'git':
                        result = str(iac / '.git/info/exclude') if '--git-path' in argv else request['iacSource']
                        return subprocess.CompletedProcess(argv,0,result,'')
                    self.assertEqual(argv[:3],['terraform',f'-chdir={root}','init'])
                    return subprocess.CompletedProcess(argv,0,'private-init-diagnostic','')

                def seal(updated_root, bundle, key):
                    self.assertEqual(updated_root,root)
                    self.assertIn('-retirement-999-1',key)
                    private = root / '.release-cell-retirement'
                    self.assertEqual((private / 'terraform-init.log').read_text(),'private-init-diagnostic')
                    self.assertEqual(private.stat().st_mode & 0o777,0o700)
                    self.assertEqual((private/'terraform-init.log').stat().st_mode & 0o777,0o600)
                    self.assertIn('**/.release-cell-retirement/',(iac/'.git/info/exclude').read_text())
                    bundle.write_bytes(b'ciphertext-only')
                    return 'd'*64

                with patch.dict(os.environ,{'GITHUB_REF':'refs/heads/trunk','GITHUB_RUN_ID':'999',
                        'GITHUB_RUN_ATTEMPT':'1'},clear=True), \
                     patch('retire_cloud_cell.subprocess.run',side_effect=command), \
                     patch('boto3.Session',return_value=session), \
                     patch.object(targets.terraform_target,'serverless',return_value=target), \
                     patch('retire_cloud_cell.importlib.util.module_from_spec',return_value=helper), \
                     patch('retire_cloud_cell.importlib.util.spec_from_file_location',return_value=Mock()), \
                     patch.object(run_cloud,'open_state',side_effect=open_original) as opened, \
                     patch.object(run_cloud,'seal_state',side_effect=seal) as sealed, \
                     patch.object(run_cloud,'forget_secret',side_effect=AssertionError('original key must remain')), \
                     patch.object(cost_meter,'cell_cost',side_effect=RuntimeError('meter unavailable')), \
                     patch('retire_cloud_cell.compute_absence',return_value={'resources':[]}) as absence:
                    if failed:
                        with self.assertRaisesRegex(RecoveryRefused,'retained failure'):
                            perform(args)
                        absence.assert_not_called()
                    else:
                        perform(args)
                        absence.assert_called_once()
                    opened.assert_called_once()
                    sealed.assert_called_once()
                    self.assertNotEqual(opened.call_args.args[2],sealed.call_args.args[2])
                    self.assertEqual(os.environ['GITHUB_RUN_ID'],'999')
                receipt = json.loads((output/'receipt.json').read_text())
                self.assertTrue(receipt['originalStateKeyRetained'])
                self.assertTrue(receipt['recoveryStateSealed'])
                self.assertFalse(receipt['costReconciled'])
                self.assertEqual(receipt['costBeforeRetirement']['status'],'unavailable')
                self.assertEqual(receipt['status'],'incomplete' if failed else 'compute-retired-cost-and-recovery-pending')
                self.assertNotIn('private-init-diagnostic',(output/'receipt.json').read_text())


if __name__ == "__main__":
    unittest.main()
