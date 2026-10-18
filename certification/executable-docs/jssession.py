"""Own one JavaScript worker; a lost interpreter can never silently restart."""
from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable


class JavaScriptSession:
    def __init__(self, command: list[str], stop: Callable[[], None]):
        self.stop = stop
        self.broken = False
        self.proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, text=True, start_new_session=True)

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

    def execute(self, path: Path, cwd: str, env: dict[str, str], language: str, timeout: float,
                baseline: dict[str, str] | None = None) -> dict:
        if self.broken:
            raise RuntimeError("JavaScript context was lost; refusing to restart or replay earlier blocks")
        response = Path(str(path) + ".response.json")
        request_id = uuid.uuid4().hex
        deadline = time.monotonic() + timeout
        try:
            response.unlink(missing_ok=True)
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps({"file": str(path), "cwd": cwd, "env": env,
                                             "language": language, "baselineEnv": baseline,
                                             "response": str(response), "id": request_id}) + "\n")
            self.proc.stdin.flush()
            # The worker atomically publishes completion on the shared workdir.
            # Reader stdout/stderr (including inherited child fds) carry no control.
            while not response.exists():
                if self.proc.poll() is not None:
                    if response.exists():
                        break  # completion may publish between the stat and exit check
                    raise RuntimeError("JavaScript worker exited; context was lost")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"JavaScript context timed out after {timeout}s; refusing to restart")
                time.sleep(min(0.01, remaining))
            answer = json.loads(response.read_text())
            if (not isinstance(answer, dict) or answer.get("file") != str(path)
                    or answer.get("id") != request_id or type(answer.get("ok")) is not bool
                    or type(answer.get("lost")) is not bool or not isinstance(answer.get("error"), str)
                    or answer["lost"] == answer["ok"]):
                raise RuntimeError("invalid JavaScript worker response; context was lost")
            if answer.get("lost"):
                self.close()
            return answer
        except (OSError, ValueError, RuntimeError):
            self.close()
            raise
