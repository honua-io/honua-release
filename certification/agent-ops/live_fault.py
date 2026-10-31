"""Arm a real migration interruption using the GA gate's journal-lock technique.

Not wired to priced trials. The trial controller must persist the receipt and
require a fired result before scoring O08; a missing receipt is never a pass.
"""
from __future__ import annotations

import re
import subprocess
import time
import uuid
from pathlib import Path

from ga_actions import _PROJECT


class MigrationFault:
    def __init__(self, project: str, compose_file: Path, *, timeout: float = 90):
        if not _PROJECT.fullmatch(project) or not compose_file.is_file() or timeout <= 0:
            raise ValueError("invalid isolated fault target")
        self.project = project
        self.compose_file = compose_file.resolve()
        self.timeout = timeout
        self.holder_name = 'honua-chaos-' + uuid.uuid4().hex
        self.holder = None
        self.holder_pid = None
        self.armed_at = None
        self.fired = None

    def _compose(self) -> list[str]:
        return ['docker', 'compose', '-p', self.project, '-f', str(self.compose_file)]

    def _sql(self, sql: str) -> str:
        completed = subprocess.run(self._compose() + ['exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1',
                                   '-U', 'honua', '-d', 'honua', '-Atc', sql],
                                   capture_output=True, text=True, timeout=10, check=False)
        if completed.returncode != 0:
            raise RuntimeError('fault database operation failed')
        return completed.stdout.strip()

    def arm(self) -> dict:
        if self.holder is not None:
            raise RuntimeError('fault already armed')
        # The same lock and transaction as partial_failure_probe in the GA gate:
        # hold SHARE on the migration journal so a real candidate transaction
        # blocks after making changes and before writing its journal row.
        hold = int(self.timeout) + 30
        sql = (f"SET application_name = '{self.holder_name}'; SET statement_timeout = '{hold + 30}s'; "
               f"BEGIN; LOCK TABLE public.schema_versions IN SHARE MODE; SELECT pg_sleep({hold}); COMMIT;")
        self.holder = subprocess.Popen(self._compose() + ['exec', '-T', 'db', 'psql', '-v', 'ON_ERROR_STOP=1',
                                       '-U', 'honua', '-d', 'honua', '-Atc', sql],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                if self.holder.poll() is not None:
                    raise RuntimeError('fault lock holder exited before arming')
                pid = self._sql("SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
                                f"WHERE a.application_name='{self.holder_name}' AND a.datname=current_database() "
                                "AND l.locktype='relation' AND l.relation='public.schema_versions'::regclass "
                                "AND l.mode='ShareLock' AND l.granted")
                if re.fullmatch(r'[1-9][0-9]*', pid):
                    self.holder_pid = int(pid)
                    self.armed_at = time.time()
                    return {'armed': True, 'at': self.armed_at, 'project': self.project}
                time.sleep(0.2)
            raise TimeoutError('fault lock was not observed')
        except Exception:
            self.close()
            raise

    def wait_and_fire(self) -> dict:
        if self.holder_pid is None or self.fired is not None:
            raise RuntimeError('fault not armed or already fired')
        deadline = time.monotonic() + self.timeout
        # Recheck the predicate in the terminating SQL itself, so a recycled PID
        # or an unrelated client cannot be killed after a stale observation.
        predicate = (f"{self.holder_pid} = ANY(pg_blocking_pids(a.pid)) AND a.wait_event_type='Lock' "
                     "AND a.query ILIKE '%insert%schema_versions%' AND a.datname=current_database() "
                     "AND a.backend_xid IS NOT NULL AND EXISTS (SELECT 1 FROM pg_locks l "
                     "WHERE l.pid=a.pid AND l.granted AND l.locktype='relation' AND l.mode IN "
                     "('RowExclusiveLock','ShareUpdateExclusiveLock','ShareLock','ShareRowExclusiveLock',"
                     "'ExclusiveLock','AccessExclusiveLock'))")
        try:
            while time.monotonic() < deadline:
                paused = self._sql('SELECT a.pid FROM pg_stat_activity a WHERE ' + predicate)
                if re.fullmatch(r'[1-9][0-9]*', paused):
                    pid = int(paused)
                    result = self._sql(f'SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE a.pid={pid} AND ' + predicate)
                    if result != 't':
                        raise RuntimeError('observed migration backend was not interrupted')
                    self.fired = {'kind': 'migration-backend-terminated', 'at': time.time(),
                                  'project': self.project, 'runner_pid': pid,
                                  'holder_pid': self.holder_pid, 'paused_with_xid_and_write_lock': True,
                                  'terminate_result': result}
                    return dict(self.fired)
                time.sleep(0.2)
            raise TimeoutError('migration fault never fired')
        finally:
            self.close()

    def close(self) -> None:
        if self.holder is None:
            return
        try:
            self._sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
                      f"application_name='{self.holder_name}' AND datname=current_database() "
                      "AND usename=current_user AND pid <> pg_backend_pid()")
        finally:
            try:
                self.holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.holder.terminate()
                self.holder.wait(timeout=5)
            self.holder = None
            self.holder_pid = None
