"""Shared prerequisites, without original primary or candidate qualification credit."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]

def test_workspace_reader_uses_exact_normal_execution_grant():
    result=subprocess.run(["python3","-B","certification/installed-clients/mcp_workspace_identity_profile_controls.py"],cwd=ROOT,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr

def test_explicit_owned_authorization_configuration_observation():
    result=subprocess.run(["python3","-B","certification/installed-clients/mcp_deployment_identity_configuration_controls.py"],cwd=ROOT,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr

def test_normal_role_fixture_ownership_and_original_receipt():
    result=subprocess.run(["python3","-B","certification/installed-clients/mcp_deployment_identity_role_controls.py"],cwd=ROOT,capture_output=True,text=True,timeout=60)
    assert result.returncode==0,result.stdout+result.stderr

def test_fixed_client_tenant_transport_and_no_tenant_compatibility():
    result=subprocess.run(["node","--test","certification/installed-clients/mcp_tenant_transport.test.mjs"],cwd=ROOT,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout+result.stderr
