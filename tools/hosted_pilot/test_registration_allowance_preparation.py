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


if __name__=='__main__':unittest.main()
