"""CLI refusal exposes only fixed action names and recognized service codes."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import deploy

class CliDiagnostics(unittest.TestCase):
    def test_actual_kms_denial_shape_never_reflects_private_message_or_request(self):
        private=b'PRIVATE_SENTINEL request-body secret_string credential'
        stderr=b'An error occurred (AccessDeniedException) when calling the ListResourceTags operation: '+private
        result=subprocess.CompletedProcess([],255,b'PRIVATE_OUTPUT',stderr)
        with patch('deploy.subprocess.run',return_value=result):
            with self.assertRaises(ValueError) as error:
                deploy.aws('kms','list-resource-tags','--key-id','PRIVATE_KEY_ID')
        self.assertEqual(str(error.exception),'aws kms list-resource-tags failed; service_error=AccessDeniedException; request/response details omitted')
        self.assertNotIn('PRIVATE',str(error.exception))
        self.assertNotIn('retained',str(error.exception))

    def test_unknown_error_code_action_and_ambiguous_headers_are_not_reflected(self):
        for args,stderr in [
            (['aws','kms','describe-key','--key-id','PRIVATE_KEY'],b'An error occurred (PRIVATE_CODE) when calling the DescribeKey operation: PRIVATE'),
            (['aws','PRIVATE_SERVICE','PRIVATE_ACTION','PRIVATE_VALUE'],b'PRIVATE_MESSAGE'),
            (['aws','kms','list-keys'],b'An error occurred (AccessDeniedException) when calling ListKeys operation: PRIVATE\nAn error occurred (PRIVATE_CODE) when calling ListKeys operation: PRIVATE'),
            (['terraform','PRIVATE_ARGUMENT'],b'PRIVATE_MESSAGE')]:
            with self.subTest(args=args[:3]):
                text=deploy.failure_diagnostic(args,stderr,retained=False)
                self.assertNotIn('PRIVATE',text)
                self.assertNotIn('retained',text)

    def test_explicit_private_log_is_retained_with_restricted_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'private.log'
            result=subprocess.CompletedProcess([],1,b'PRIVATE_PLAN',b'PRIVATE_RESPONSE')
            with patch('deploy.subprocess.run',return_value=result):
                with self.assertRaises(ValueError) as error:
                    deploy.run(['terraform','plan','-var=PRIVATE_REQUEST'],log=path)
            self.assertEqual(path.read_bytes(),b'PRIVATE_PLANPRIVATE_RESPONSE')
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertEqual(str(error.exception),'terraform plan failed; private diagnostic retained only on executor')

    def test_success_returns_original_private_bytes_without_failure_processing(self):
        result=subprocess.CompletedProcess([],0,b'{"private":"original"}',b'')
        with patch('deploy.subprocess.run',return_value=result),patch('deploy.failure_diagnostic',side_effect=AssertionError('success must not parse diagnostics')):
            self.assertEqual(deploy.run(['aws','kms','describe-key']),result.stdout)
