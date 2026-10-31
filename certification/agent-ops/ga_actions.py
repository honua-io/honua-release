"""Invoke the Compose GA gate's existing procedures against one trial project.

This adapter is not wired to priced execution. The caller must supply its own
fresh trial stack, isolated project, immutable images, and evidence directory.
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

_GATE = Path(__file__).resolve().parents[2] / "e2e" / "harness" / "upgrade-chaos.sh"
_FUNCTIONS = (
    "compose", "db_exec", "wait_db", "server_ready", "wait_ready", "wait_ready_attempts",
    "stop_server", "cu_start_server", "cu_wait_ready", "cu_restore_backup", "cu_schema",
)
_PROJECT = re.compile(r"ops-[0-9a-f]{32}\Z")
_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}\Z")


def gate_functions(source: str) -> str:
    """Copy only named top-level functions, never the gate's whole-stack driver."""
    functions = []
    for name in _FUNCTIONS:
        # compose is a compact one-line function; the others have a closing brace on its own line.
        pattern = (rf"^compose\(\) \{{ [^\n]+\}}$" if name == "compose" else
                   rf"^{name}\(\) \{{\n.*?^\}}$")
        found = list(re.finditer(pattern, source, flags=re.MULTILINE | re.DOTALL))
        if len(found) != 1:
            raise ValueError(f"GA procedure {name} missing or ambiguous")
        functions.append(found[0].group())
    return "\n\n".join(functions)


def invoke(*, operation: str, project: str, compose_file: Path, output: Path,
           prior_image: str, candidate_image: str, base_url: str,
           timeout: float = 240, expected_backup_sha256: str | None = None) -> dict:
    if operation not in {"backup", "start-candidate", "restore-prior", "schema"}:
        raise ValueError("unsupported GA action")
    if not _PROJECT.fullmatch(project):
        raise ValueError("invalid trial project")
    if not _IMAGE.fullmatch(prior_image) or not _IMAGE.fullmatch(candidate_image) or prior_image == candidate_image:
        raise ValueError("distinct immutable prior and candidate images required")
    compose_file = compose_file.resolve(strict=True)
    output = output.resolve(strict=True)
    if not compose_file.is_file() or not output.is_dir():
        raise ValueError("trial compose file or evidence directory missing")
    backup = output / "compose-pre-upgrade.dump"
    if operation == "restore-prior":
        if not isinstance(expected_backup_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_backup_sha256):
            raise ValueError("restore requires the independently retained backup digest")
        with backup.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected_backup_sha256:
            raise ValueError("backup digest mismatch")
    source = _GATE.read_text(encoding="utf-8")
    functions = gate_functions(source)
    # Guard the upstream restore helper's start step: it otherwise starts the
    # old server even when pg_restore reported a nonzero exit in its log.
    functions = functions.replace("cu_start_server() {", "_ga_cu_start_server() {", 1)
    # No model-supplied shell is evaluated. All variable values are positional
    # arguments, and docker compose is explicitly scoped to the trial project.
    prelude = r'''set -Eeuo pipefail
COMPOSE_FILE="$1"; PROJECT="$2"; OUT="$3"; PRIOR_IMAGE="$4"; CANDIDATE_IMAGE="$5"; E2E_BASE="$6"
export E2E_BASE COMPOSE_PROJECT_NAME="$PROJECT"
COMPOSE=(docker compose -p "$PROJECT" -f "$COMPOSE_FILE")
CU_BACKUP="$OUT/compose-pre-upgrade.dump"
verify_image() {
  local ref="$1" expected containers id
  expected="$(docker image inspect --format '{{.Id}}' "$ref")"
  test -n "$expected"
  containers="$(compose ps -q --status running server)"
  test -n "$containers"
  while IFS= read -r id; do
    test -n "$id" && test "$(docker inspect --format '{{.Image}}' "$id")" = "$expected"
  done <<< "$containers"
}
'''
    operations = {
        "backup": r'''test ! -e "$CU_BACKUP"
compose exec -T db pg_dump -U honua -d honua -Fc > "$CU_BACKUP.tmp"
test -s "$CU_BACKUP.tmp"
mv "$CU_BACKUP.tmp" "$CU_BACKUP"''',
        "start-candidate": r'''cu_start_server "$CANDIDATE_IMAGE"
if ! cu_wait_ready "${CHAOS_READY_ATTEMPTS:-90}"; then
  compose logs server > "$OUT/compose-contract-gate.log" 2>&1
  nonce="$(grep -hoE 'HONUA_APPROVE_CONTRACT_MIGRATIONS=[0-9a-f]{8,64}' "$OUT/compose-contract-gate.log" | head -n 1 | cut -d= -f2)" || nonce=""
  test -n "$nonce"
  umask 077
  printf 'services:\n  server:\n    environment:\n      HONUA_APPROVE_CONTRACT_MIGRATIONS: "%s"\n' "$nonce" > "$OUT/compose-approve-contract.override.yml"
  stop_server
  cu_start_server "$CANDIDATE_IMAGE" "$OUT/compose-approve-contract.override.yml"
  wait_ready
fi
verify_image "$CANDIDATE_IMAGE"''',
        # The upstream helper logs pg_restore's exit code rather than propagating it.
        # Require its exact success line as well as the helper's readiness result.
        "restore-prior": r'''test -s "$CU_BACKUP"
compose stop server >/dev/null
test -z "$(compose ps -q --status running server)"
cu_restore_backup
grep -Fx 'pg_restore exit 0' "$OUT/compose-restore.log" >/dev/null
verify_image "$PRIOR_IMAGE"''',
        "schema": "cu_schema",
    }
    guard = r'''
server_ready() {
  local containers id
  containers="$(compose ps -q --status running server)"
  test -n "$containers"
  while IFS= read -r id; do
    test -n "$id" && docker exec "$id" wget -q -T 5 -O /dev/null http://127.0.0.1:8080/healthz/ready || return 1
  done <<< "$containers"
}
cu_start_server() {
  if [ "${RESTORING:-0}" = 1 ]; then
    grep -Fx 'pg_restore exit 0' "$OUT/compose-restore.log" >/dev/null || return 1
  fi
  _ga_cu_start_server "$@"
}
'''
    if operation == "restore-prior":
        guard += "RESTORING=1\n"
    script = prelude + functions + guard + "\n" + operations[operation] + "\n"
    result = subprocess.run(["bash", "-c", script, "ops-ga-action", str(compose_file), project,
                             str(output), prior_image, candidate_image, base_url],
                            env=os.environ.copy(), capture_output=True, text=True,
                            timeout=timeout, check=False)
    note = {"ok": result.returncode == 0, "operation": operation, "project": project,
            "gate_sha256": hashlib.sha256(source.encode()).hexdigest(), "exit_code": result.returncode}
    if operation == "backup" and result.returncode == 0:
        with backup.open("rb") as stream:
            note["backup_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    if operation == "schema" and result.returncode == 0:
        schema = result.stdout.strip()
        if not schema.isdigit():
            raise ValueError("schema procedure returned invalid data")
        note["schema"] = int(schema)
    return note
