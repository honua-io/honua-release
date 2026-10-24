"""Controller routing controls; no native/cloud/financial effects."""
from types import SimpleNamespace
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import registration_allowance_preparation as preparation
from agent_e2e_pack import canonical_digest


class RegistrationPreparation(unittest.TestCase):
    def test_fixed_entry_loads_dependencies_without_an_ambient_python_path(self):
        with tempfile.TemporaryDirectory() as directory:
            environment = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
            result = subprocess.run([sys.executable, str(Path(preparation.__file__).resolve()), '--help'],
                cwd=directory, env=environment, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--bundle', result.stdout)

    def setUp(self):
        self.config={'operation':{'operationId':'original-preparation','reservationIds':{'infrastructure':'prep-infra','cleanup':'prep-cleanup'}},
            'registration':{'candidateKey':'sha256:'+'a'*64,'operation':{'operationId':'runtime-registration-r',
                'reservationIds':{'infrastructure':'reg-infra','cleanup':'reg-cleanup'}},'maximumUsd':{'infrastructure':'1.00','cleanup':'1.00'}}}
        self.row={'schema':'honua.runtime-registration-admission/v1','deadlineEpoch':2000000600}
        self.key=preparation.admission_key(self.config['registration'])
        self.original={'producer':'private-runtime-registration-admission','candidateKey':self.config['registration']['candidateKey'],'value':self.row}
        self.durable=SimpleNamespace(state={'scenarioOperations':{self.key:self.original}})

    def test_original_admission_reference_is_returned_without_activation(self):
        reader=MagicMock()
        with patch.object(preparation,'authorize') as guard, patch.object(preparation,'retain_admission_and_reservations',return_value=self.row) as producer:
            result=preparation.provision(self.durable,self.config,source_reader=reader)
            self.assertEqual(result['key'],self.key);self.assertEqual(result['sha256'],canonical_digest(self.original))
            self.assertFalse(result['activation']);self.assertFalse(result['qualification'])
            producer.assert_called_once();self.assertIs(producer.call_args.kwargs['source_reader'],reader)
            producer.call_args.kwargs['guard']()
            guard.assert_called_with(self.durable,self.config['operation'])

    def test_registration_cannot_reuse_preparation_operation(self):
        self.config['registration']['operation']['operationId']='original-preparation'
        with patch.object(preparation,'retain_admission_and_reservations') as producer:
            with self.assertRaisesRegex(Exception,'reuse'):preparation.provision(self.durable,self.config)
            producer.assert_not_called()

    def test_registration_cannot_reuse_either_preparation_reservation(self):
        self.config['registration']['operation']['reservationIds']['cleanup']='prep-infra'
        with patch.object(preparation,'retain_admission_and_reservations') as producer:
            with self.assertRaisesRegex(Exception,'reuse'):preparation.provision(self.durable,self.config)
            producer.assert_not_called()

    def test_lost_original_protected_custody_is_not_projected_as_budget_success(self):
        with patch.object(preparation,'authorize'), patch.object(preparation,'retain_admission_and_reservations',return_value={'changed':'admission'}):
            with self.assertRaisesRegex(Exception,'original protected custody'):preparation.provision(self.durable,self.config)


class DefaultPreparationSource(unittest.TestCase):
    def test_default_reader_accepts_actual_tracked_original_history_and_funding_namespaces(self):
        for relative in ('certification/hosted-pilot/history/20261023T161500Z.json',
                         'certification/agent-e2e/foss4g-pilot.v1.json'):
            path=preparation.ROOT/relative
            with self.subTest(path=relative):
                self.assertEqual(preparation.checked_source(relative,preparation.digest(path.read_bytes())),path)
    def test_default_reader_still_refuses_changed_digest_and_escaped_namespaces(self):
        with self.assertRaisesRegex(Exception,'bytes differ'):
            preparation.checked_source('certification/hosted-pilot/history/20261023T161500Z.json','sha256:'+'0'*64)
        for relative in ('tools/private.json','certification/hosted-pilot/history/../private.json','/tmp/unreviewed.json'):
            with self.subTest(path=relative),self.assertRaisesRegex(Exception,'namespace'):
                preparation.checked_source(relative,'sha256:'+'0'*64)
    def test_default_reader_still_requires_original_tracked_files(self):
        from unittest.mock import patch
        import subprocess
        relative='certification/hosted-pilot/history/20261023T161500Z.json'
        path=preparation.ROOT/relative
        with patch.object(preparation,'git',side_effect=subprocess.CalledProcessError(1,['git','ls-files'])):
            with self.assertRaises(subprocess.CalledProcessError):
                preparation.checked_source(relative,preparation.digest(path.read_bytes()))

if __name__=='__main__':unittest.main()
