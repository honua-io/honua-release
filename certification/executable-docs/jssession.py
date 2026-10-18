"""Own one JavaScript worker; a lost interpreter can never silently restart."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable


class JavaScriptSession:
    def __init__(self, command: list[str], stop: Callable[[], None]):
        self.stop = stop
        self.broken = False
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def close(self) -> None:
        if self.broken:
            return
        self.broken = True
        try:
            self.stop()
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
            self.proc.wait()

    def execute(self, path: Path, cwd: str, env: dict[str, str], language: str, timeout: float) -> dict:
        if self.broken:
            raise RuntimeError("JavaScript context was lost; refusing to restart or replay earlier blocks")
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps({"file": str(path), "cwd": cwd, "env": env,
                                             "language": language}) + "\n")
            self.proc.stdin.flush()
            line = self.lines.get(timeout=timeout)
            if line is None:
                raise RuntimeError("JavaScript worker exited; context was lost")
            answer = json.loads(line)
            if answer.get("file") != str(path) or type(answer.get("ok")) is not bool:
                raise RuntimeError("invalid JavaScript worker response; context was lost")
            if answer.get("lost"):
                self.close()
            return answer
        except queue.Empty:
            self.close()
            raise TimeoutError(f"JavaScript context timed out after {timeout}s; refusing to restart") from None
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise
