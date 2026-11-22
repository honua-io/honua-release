"""Admit GA forward upgrades only with an observed, stable candidate restart."""
from compose_upgrade_version import version_evidence


RESTART_CHECKS = ("ready", "identityMatched", "journalUnchanged", "seededRowsUnchanged",
                  "preservationReverified")


def forward_evidence(pair: dict, observed: dict, phases: dict) -> dict:
    """A backup and actual restart prove forward recovery, never old-image compatibility."""
    if pair.get("candidate_version") != "2026.1.0":
        return {"status": "not-applicable", "why": "not the 2026.1.0 GA upgrade"}
    result = {"status": "fail", "policy": "forward-only"}
    if observed.get("migrationPolicy") != "forward-only":
        return {**result, "why": "forward-only migration policy is missing"}
    steps = observed.get("customerSteps")
    if not isinstance(steps, dict) or steps.get("preUpgradeBackup") is not True:
        return {**result, "why": "pre-upgrade backup was not recorded"}
    restart = observed.get("candidateRestart")
    if not isinstance(restart, dict) or any(restart.get(key) is not True for key in RESTART_CHECKS):
        return {**result, "why": "candidate restart did not prove readiness, idempotency and preservation"}
    identity = version_evidence(pair, {"candidate": phases.get("restart")})
    if identity["status"] != "pass":
        return {**result, "why": f"candidate restart identity refused: {identity['why']}"}
    return {**result, "status": "pass", "checks": dict.fromkeys(RESTART_CHECKS, True),
            "why": "candidate restarted with matching identity, unchanged journal and preserved data"}
