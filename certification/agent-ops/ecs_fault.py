"""Arm the migration-journal lock on a cell database the caller can already query.

The receipt is evidence only when the waiter was observed and the terminating
statement itself rechecked the lock. A missing waiter is not a fired fault.
"""
from __future__ import annotations

import re
import time
import uuid

_NAME = re.compile(r"honua-chaos-[0-9a-f]{32}\Z")


class JournalFault:
    def __init__(self, session, evidence_id: str, target_pid: int, read_migration_state, hold_seconds: int = 30):
        if not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{32}", evidence_id):
            raise ValueError("evidence id")
        if not isinstance(hold_seconds, int) or not 1 <= hold_seconds <= 120:
            raise ValueError("hold")
        if type(target_pid) is not int or target_pid <= 0 or not callable(read_migration_state):
            raise ValueError("migration target and controller read required")
        self.target_pid = target_pid
        self.read_migration_state = read_migration_state
        self.session = session
        self.evidence_id = evidence_id
        self.hold_seconds = hold_seconds
        self.holder_name = "honua-chaos-" + uuid.uuid4().hex
        self._armed = False
        self._pid = None

    def arm(self) -> dict:
        if self._armed:
            raise RuntimeError("fault already armed")
        self._armed = True
        hold = self.hold_seconds + 30
        sql = (
            f"SET application_name = '{self.holder_name}'; "
            f"SET statement_timeout = '{hold + 30}s'; "
            "BEGIN; LOCK TABLE public.schema_versions IN SHARE MODE; "
            f"SELECT pg_sleep({hold}); COMMIT;"
        )
        self.session.start(sql)
        pid = self.session.query(
            "SELECT l.pid FROM pg_locks l JOIN pg_stat_activity a ON a.pid=l.pid "
            f"WHERE a.application_name='{self.holder_name}' AND l.locktype='relation' "
            "AND l.relation='public.schema_versions'::regclass AND l.mode='ShareLock' AND l.granted"
        )
        if not re.fullmatch(r"[1-9][0-9]*", pid or ""):
            self.session.stop()
            raise TimeoutError("fault lock was not observed")
        self._pid = int(pid)
        return {"armed": True, "holder": self.holder_name}

    def wait_and_fire(self) -> dict:
        if self._pid is None:
            raise RuntimeError("fault not armed")
        predicate = (
            f"a.pid={self.target_pid} AND a.datname=current_database() "
            f"AND {self._pid} = ANY(pg_blocking_pids(a.pid)) AND a.wait_event_type='Lock' "
            "AND a.query ILIKE '%insert%schema_versions%' "
            f"AND EXISTS (SELECT 1 FROM pg_stat_activity h JOIN pg_locks l ON l.pid=h.pid "
            f"WHERE h.pid={self._pid} AND h.datname=current_database() "
            f"AND h.application_name='{self.holder_name}' AND l.locktype='relation' "
            "AND l.relation='public.schema_versions'::regclass AND l.mode='ShareLock' AND l.granted)"
        )
        deadline = time.monotonic() + self.hold_seconds
        started = False
        try:
            while time.monotonic() < deadline:
                waiter = self.session.query(f"SELECT a.pid FROM pg_stat_activity a WHERE {predicate}")
                if waiter == str(self.target_pid):
                    started = True
                    terminated = self.session.query(
                        "SELECT pg_terminate_backend(a.pid) FROM pg_stat_activity a WHERE " + predicate)
                    if terminated.strip().lower() not in {"t", "true"}:
                        break
                    while time.monotonic() < deadline:
                        absent = self.session.query(
                            f"SELECT count(*) FROM pg_stat_activity WHERE pid={self.target_pid} "
                            "AND datname=current_database()")
                        state = self.read_migration_state()
                        if (absent == "0" and isinstance(state, dict)
                                and state.get("evidence_id") == self.evidence_id
                                and state.get("backend_pid") == self.target_pid
                                and state.get("migration_failed") is True
                                and state.get("journal_absent") is True):
                            return {"fired": True, "upgrade_started": True,
                                    "evidence_id": self.evidence_id, "holder": self.holder_name,
                                    "backend_pid": self.target_pid, "interrupted": True}
                        time.sleep(0.2)
                    break
                time.sleep(0.2)
        finally:
            self.session.stop()
        return {"fired": False, "upgrade_started": started}
