"""Arm a real migration interruption using the GA gate's journal-lock technique.

Not wired to priced trials. The trial controller must persist the receipt and
require a fired result before scoring O08; a missing receipt is never a pass.
"""
from __future__ import annotations

import re
import hashlib
import ipaddress
import os
import subprocess
import time
import uuid
from pathlib import Path

from ga_actions import _PROJECT


class MigrationFault:
    def __init__(self, project: str, compose_file: Path, *, pending_migrations: tuple[str, ...], candidate_image_id: str,
                 timeout: float = 90, sensitive_values: tuple[str, ...] = ()):
        if (not _PROJECT.fullmatch(project) or not compose_file.is_file() or timeout <= 0 or
            not re.fullmatch(r"sha256:[a-f0-9]{64}", candidate_image_id) or not pending_migrations or
            any(not re.fullmatch(r"[A-Za-z0-9._-]+\.sql", x) for x in pending_migrations)):
            raise ValueError("invalid isolated fault target or pending migration set")
        self.project = project
        self.compose_file = compose_file.resolve()
        self.timeout = timeout
        self.pending_migrations = frozenset(pending_migrations)
        self.candidate_image_id = candidate_image_id
        self.sensitive_values = sensitive_values
        self.holder_name = 'honua-chaos-' + uuid.uuid4().hex
        self.holder = None
        self.holder_pid = None
        self.used = False
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

    def _candidate_address(self) -> tuple[str, str] | None:
        ps = subprocess.run(self._compose() + ['ps', '-q', '--status', 'running', 'server'],
                            capture_output=True, text=True, timeout=10, check=False)
        ids = ps.stdout.split()
        if ps.returncode != 0 or len(ids) != 1 or not re.fullmatch(r'[a-f0-9]{12,64}', ids[0]):
            return None
        inspect = subprocess.run(['docker', 'inspect', '--format',
                                  '{{.Image}}|{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', ids[0]],
                                 capture_output=True, text=True, timeout=10, check=False)
        image, sep, address = inspect.stdout.strip().partition('|')
        try:
            ipaddress.IPv4Address(address)
        except ValueError:
            return None
        if inspect.returncode != 0 or not sep or image != self.candidate_image_id:
            return None
        return ids[0], address

    def _logs(self) -> str:
        result = subprocess.run(self._compose() + ['logs', 'server'], capture_output=True,
                                text=True, timeout=10, check=False)
        if result.returncode != 0:
            raise RuntimeError('candidate logs unavailable')
        return result.stdout

    def arm(self) -> dict:
        if self.used:
            raise RuntimeError('fault already armed')
        self.used = True
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
                     "AND EXISTS (SELECT 1 FROM pg_stat_activity h JOIN pg_locks hl ON hl.pid=h.pid "
                     f"WHERE h.pid={self.holder_pid} AND h.application_name='{self.holder_name}' "
                     "AND h.datname=current_database() AND hl.locktype='relation' "
                     "AND hl.relation='public.schema_versions'::regclass "
                     "AND hl.mode='ShareLock' AND hl.granted) "
                     "AND a.query ILIKE '%insert%schema_versions%' AND a.datname=current_database() "
                     "AND a.backend_xid IS NOT NULL AND EXISTS (SELECT 1 FROM pg_locks l "
                     "WHERE l.pid=a.pid AND l.granted AND l.locktype='relation' AND l.mode IN "
                     "('RowExclusiveLock','ShareUpdateExclusiveLock','ShareLock','ShareRowExclusiveLock',"
                     "'ExclusiveLock','AccessExclusiveLock'))")
        try:
            while time.monotonic() < deadline:
                candidate = self._candidate_address()
                if candidate is None:
                    time.sleep(0.2)
                    continue
                container_id, address = candidate
                candidate_predicate = predicate + f" AND a.client_addr = '{address}'::inet"
                paused = self._sql('SELECT a.pid FROM pg_stat_activity a WHERE ' + candidate_predicate)
                if re.fullmatch(r'[1-9][0-9]*', paused):
                    pid = int(paused)
                    result = self._sql(f'SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE a.pid={pid} AND ' + candidate_predicate)
                    if result != 't':
                        raise RuntimeError('observed migration backend was not interrupted')
                    # Signal delivery alone is not a fired fault. As in the GA
                    # gate, require the blocked migration script, a dead backend,
                    # failure logs and absence of the journal row.
                    while time.monotonic() < deadline:
                        count = self._sql(f"SELECT count(*) FROM pg_stat_activity WHERE pid={pid} AND datname=current_database()")
                        logs = self._logs()
                        matches = re.findall(r"Executing Database Server script '([^']+)'", logs)
                        script = matches[-1] if matches else None
                        failure = re.search(r'migration[^\n]{0,200}?fail', logs, re.I)
                        if count == '0' and script in self.pending_migrations and failure:
                            journal = self._sql(f"SELECT count(*) FROM public.schema_versions WHERE scriptname='{script}'")
                            if journal != '0':
                                raise RuntimeError('interrupted migration was journaled')
                            from client import Redactor
                            redactor = Redactor(sensitive_values=[*self.sensitive_values, *(os.environ.get(name, '') for name in (
                                'E2E_API_KEY', 'TERMINAL_MODEL_API_KEY', 'HONUA_ADMIN_PASSWORD',
                                'HONUA_CANARY_AWS_ACCESS_KEY_ID', 'HONUA_CANARY_AWS_SECRET_ACCESS_KEY',
                                'HONUA_CANARY_AWS_SESSION_TOKEN', 'HONUA_CANARY_TRANSCRIPT_SIGNING_SEED',
                            ))])
                            excerpt = redactor.text(f"Executing Database Server script '{script}'\n{failure.group()}\n")
                            self.fired = {'kind': 'migration-backend-terminated', 'at': time.time(),
                                          'project': self.project, 'candidate_container': container_id,
                                          'candidate_image_id': self.candidate_image_id, 'runner_pid': pid,
                                          'holder_pid': self.holder_pid, 'script': script,
                                          'paused_with_xid_and_write_lock': True, 'terminate_result': result,
                                          'backend_absent': True, 'journal_absent': True,
                                          'log_excerpt': excerpt,
                                          'log_excerpt_sha256': hashlib.sha256(excerpt.encode()).hexdigest()}
                            return dict(self.fired)
                        time.sleep(0.2)
                    raise TimeoutError('migration termination was not independently observed')
                time.sleep(0.2)
            raise TimeoutError('migration fault never fired')
        finally:
            try:
                self.close()
            except Exception:
                self.fired = None
                raise

    def close(self) -> None:
        if self.holder is None:
            return
        try:
            self._sql("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE "
                      f"application_name='{self.holder_name}' AND datname=current_database() "
                      "AND usename=current_user AND pid <> pg_backend_pid()")
            deadline = time.monotonic() + 10
            while True:
                remaining = self._sql("SELECT count(*) FROM pg_stat_activity WHERE "
                                      f"application_name='{self.holder_name}' AND datname=current_database() "
                                      "AND usename=current_user")
                if remaining == '0':
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError('fault lock holder cleanup not proven')
                time.sleep(0.2)
        finally:
            try:
                self.holder.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.holder.terminate()
                self.holder.wait(timeout=5)
            self.holder = None
            self.holder_pid = None
