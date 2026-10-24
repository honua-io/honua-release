"""Real local process controls; no Terraform apply, AWS or cleanup evidence."""
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import deploy

class CleanupProcessBound(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.log=self.root/'private.log'
        self.patch=patch.object(deploy,'TF',self.root);self.patch.start();self.addCleanup(self.patch.stop)
    def test_fast_original_process_completes_with_private_diagnostics(self):
        output=deploy.bounded_cleanup_apply([sys.executable,'-c','print("synthetic completed")'],
            log=self.log,deadline=time.time()+2)
        self.assertIn('synthetic completed',output)
        self.assertEqual(self.log.stat().st_mode&0o777,0o600)
    def test_elapsed_original_deadline_cannot_start_another_process(self):
        with patch.object(deploy.subprocess,'Popen') as start:
            with self.assertRaisesRegex(ValueError,'before apply'):
                deploy.bounded_cleanup_apply([sys.executable,'-c','pass'],log=self.log,deadline=time.time()-1)
            start.assert_not_called()
    def test_deadline_interrupts_actual_child_and_is_never_success(self):
        code='import signal,time;signal.signal(signal.SIGINT,lambda *_:exit(0));print("synthetic started",flush=True);time.sleep(30)'
        with self.assertRaisesRegex(ValueError,'deadline elapsed'):
            deploy.bounded_cleanup_apply([sys.executable,'-c',code],log=self.log,deadline=time.time()+0.2,grace=(1,1,1))
        self.assertIn('synthetic started',self.log.read_text())
    def test_actual_cancellation_stops_group_and_restores_original_handler(self):
        previous=signal.getsignal(signal.SIGTERM)
        timer=threading.Timer(0.2,lambda:os.kill(os.getpid(),signal.SIGTERM));timer.start()
        try:
            with self.assertRaisesRegex(ValueError,'interrupted'):
                deploy.bounded_cleanup_apply([sys.executable,'-c','import time;time.sleep(30)'],
                    log=self.log,deadline=time.time()+5,grace=(1,1,1))
        finally:timer.cancel();timer.join()
        self.assertIs(signal.getsignal(signal.SIGTERM),previous)
