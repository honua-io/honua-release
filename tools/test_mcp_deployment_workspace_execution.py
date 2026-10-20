"""Original deployment/workspace primary execution joins the maintained source floor."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_release_owned_deployment_workspace_semantics_and_independent_observer():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_deployment_workspace_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


def test_release_owned_deployment_workspace_complete_dependency_and_proof_binding():
    result = subprocess.run(["node", "--test", "certification/installed-clients/mcp_deployment_workspace_validation.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=360)
    assert result.returncode == 0, result.stdout + result.stderr


def test_owned_two_client_coordinator_preserves_cleanup_failures():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_deployment_identity_fixture_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


def test_original_two_client_role_and_managed_key_cohort_join():
    result = subprocess.run(["python3", "-B", "certification/installed-clients/mcp_deployment_identity_validation_controls.py"],
                            cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert result.returncode == 0, result.stdout + result.stderr


def test_complete_original_two_client_managed_authority_and_mutations():
    for name in ("mcp_deployment_workspace_full_controls.py", "mcp_deployment_workspace_full_mutation_controls.py"):
        result = subprocess.run(["python3", "-B", "certification/installed-clients/" + name],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        assert result.returncode == 0, result.stdout + result.stderr


def test_complete_js_cohort_verifier_and_partial_setup_cleanup_orchestration():
    result = subprocess.run(["node", "--test", "--test-concurrency=1", "certification/installed-clients/mcp_deployment_workspace_cohort.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True, timeout=360)
    assert result.returncode == 0, result.stdout + result.stderr
