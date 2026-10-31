"""Arm the migration-journal lock on a cell database the caller can already query.

The receipt is evidence only when the waiter was observed and the terminating
statement itself rechecked the lock. A missing waiter is not a fired fault.
"""
from __future__ import annotations

import re
import uuid

_NAME = re.compile(r"honua-chaos-[0-9a-f]{32}\Z")


class JournalFault:
    def __init__(self, session, evidence_id: str, hold_seconds: int = 30):
        if not isinstance(evidence_id, str) or not re.fullmatch(r"[0-9a-f]{32}", evidence_id):
            raise ValueError("evidence id")
        if not isinstance(hold_seconds, int) or not 1 <= hold_seconds <= 120:
            raise ValueError("hold")
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
            f"{self._pid} = ANY(pg_blocking_pids(a.pid)) AND a.wait_event_type='Lock' "
            "AND a.query ILIKE '%insert%schema_versions%' "
            f"AND EXISTS (SELECT 1 FROM pg_stat_activity h WHERE h.pid={self._pid} "
            f"AND h.application_name='{self.holder_name}')"
        )
        waiter = self.session.query(f"SELECT a.pid FROM pg_stat_activity a WHERE {predicate}")
        if not re.fullmatch(r"[1-9][0-9]*", waiter or ""):
            self.session.stop()
            return {"fired": False, "upgrade_started": False}
        terminated = self.session.query(
            "SELECT pg_terminate_backend(l.pid) FROM pg_locks l "
            "JOIN pg_stat_activity a ON a.pid=l.pid "
            f"WHERE l.pid={self._pid} AND a.application_name='{self.holder_name}' "
            "AND l.relation='public.schema_versions'::regclass AND l.granted "
            f"AND EXISTS (SELECT 1 FROM pg_stat_activity w WHERE {predicate})"
        )
        self.session.stop()
        fired = terminated.strip().lower() in {"t", "true"}
        if not fired or not _NAME.fullmatch(self.holder_name):
            return {"fired": False, "upgrade_started": False}
        return {
            "fired": True,
            "upgrade_started": True,
            "evidence_id": self.evidence_id,
            "holder": self.holder_name,
        }
