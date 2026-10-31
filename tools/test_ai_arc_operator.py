"""honua-devops operator install, credentials and stdio MCP surface, with a fake release archive."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_arc_operator as operator  # noqa: E402
import ai_arc_runner as arc_runner  # noqa: E402

FAKE_SERVER = '''#!{python}
import json, os, sys
assert sys.argv[1:] == ["--mcp"]
for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {{"protocolVersion": "2025-03-26", "capabilities": {{"tools": {{}}}},
                  "serverInfo": {{"name": "honua-devops", "version": "2026.1.1"}}}}
    elif method == "tools/list":
        names = ["install_honua", "describe_environment", "analyze_customer_requirements",
                 "recommend_deployment_topology", "provision_infrastructure", "install_handoff",
                 "verify_install_handoff"]
        result = {{"tools": [{{"name": n, "inputSchema": {{"type": "object"}}}} for n in names]}}
    else:
        result = {{"content": [{{"type": "text", "text": "environment described"}}], "isError": False}}
    print(json.dumps({{"jsonrpc": "2.0", "id": msg["id"], "result": result}}), flush=True)
'''


def fake_release(tmp_path):
    script = FAKE_SERVER.format(python=sys.executable).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as tar:
        info = tarfile.TarInfo('Honua.DevOps.Agent')
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    archive = buffer.getvalue()
    digest = hashlib.sha256(archive).hexdigest()
    spec = {'repo': 'honua-io/honua-devops', 'tag': 'v2026.1.1', 'commit': 'a' * 40,
            'assets': {'linux-x64': {'name': 'honua-devops-linux-x64.tar.gz', 'sha256': digest}}}
    calls = []

    def download(repo, tag, name, dest, published=digest):
        calls.append((repo, tag, name))
        (Path(dest) / name).write_bytes(archive if name.endswith('.gz') else f'{published}  x\n'.encode())
    return spec, download, calls


CREDS = {'AWS_ACCESS_KEY_ID': 'ASIAEXAMPLEEXAMPLE12', 'AWS_SECRET_ACCESS_KEY': 'secret-' + 'x' * 30,
         'AWS_SESSION_TOKEN': 'token-' + 'y' * 40, 'AWS_REGION': 'us-east-1', 'expiresAt': '2026-11-02T12:00:00Z'}


def test_install_verifies_the_pinned_archive_and_serves_the_operator(tmp_path):
    spec, download, calls = fake_release(tmp_path)
    installed = operator.install(spec, tmp_path / 'op', download=download)
    assert calls == [('honua-io/honua-devops', 'v2026.1.1', 'honua-devops-linux-x64.tar.gz'),
                     ('honua-io/honua-devops', 'v2026.1.1', 'honua-devops-linux-x64.tar.gz.sha256')]
    assert installed['archiveSha256'] == 'sha256:' + spec['assets']['linux-x64']['sha256']
    assert Path(installed['binary']).name == 'Honua.DevOps.Agent'


def test_install_refuses_a_mismatched_archive(tmp_path):
    spec, download, _ = fake_release(tmp_path)
    spec['assets']['linux-x64']['sha256'] = '0' * 64
    with pytest.raises(operator.OperatorError):
        operator.install(spec, tmp_path / 'op', download=download)


def test_credentials_must_be_short_lived(tmp_path):
    path = tmp_path / 'creds.json'
    path.write_text(json.dumps(CREDS))
    assert operator.slot_credentials(str(path))['AWS_SESSION_TOKEN'].startswith('token-')
    for bad in ({**CREDS, 'AWS_ACCESS_KEY_ID': 'AKIAEXAMPLEEXAMPLE12'}, {**CREDS, 'AWS_SESSION_TOKEN': ''},
                {k: v for k, v in CREDS.items() if k != 'expiresAt'}):
        path.write_text(json.dumps(bad))
        with pytest.raises(operator.OperatorError):
            operator.slot_credentials(str(path))


def test_registration_is_the_customer_command():
    assert operator.registration_command('claude-cli', Path('/o/Honua.DevOps.Agent')) == \
        ['claude', 'mcp', 'add', 'honua-devops', '--', '/o/Honua.DevOps.Agent', '--mcp']
    assert operator.registration_command('codex-cli', Path('/o/x'))[0] == 'codex'


def test_arc_pins_the_operator_release():
    arc = arc_runner.load_arc()
    spec = arc['operator']
    assert (spec['repo'], spec['tag'], spec['entryTool'], spec['toolCount']) == \
        ('honua-io/honua-devops', 'v2026.1.2', 'install_honua', 28)
    assert spec['containerImage'].startswith('ghcr.io/honua-io/honua-devops@sha256:')
    operator.pinned_asset(spec)


CONTRACT = {'schema_version': 'v1', 'kind': 'honua.iac.deploy-identity',
            'role_arn': 'arn:aws:iam::123456789012:role/honua-deploy-ecs-small',
            'topology': {'compute': 'ecs', 'redis': False, 'custom_domain': False, 'waf': False, 'gp_batch': False},
            'actions_by_service': {'ecs': ['ecs:CreateService'], 'rds': ['rds:CreateDBInstance']}}


def test_deploy_identity_contract_is_checked_and_bound_to_the_session(tmp_path):
    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(CONTRACT))
    contract = operator.deploy_identity(str(path))
    assert contract['roleArn'].endswith('honua-deploy-ecs-small') and contract['topology']['compute'] == 'ecs'
    env = operator.operator_environment(contract, {'AWS_ACCESS_KEY_ID': 'ASIAEXAMPLEEXAMPLE12', 'AWS_SECRET_ACCESS_KEY': 's',
                                                   'AWS_SESSION_TOKEN': 't', 'roleArn': CONTRACT['role_arn']})
    assert env[operator.CONTRACT_VARIABLE] == str(path.resolve()) and 'roleArn' not in env
    with pytest.raises(operator.OperatorError):
        operator.operator_environment(contract, {'roleArn': 'arn:aws:iam::123456789012:role/admin'})
    for bad in ({**CONTRACT, 'kind': 'other'}, {**CONTRACT, 'role_arn': 'arn:aws:iam::1:user/x'},
                {**CONTRACT, 'actions_by_service': {'iam': ['iam:CreateAccessKey']}},
                {**CONTRACT, 'topology': {'compute': 'ecs'}}):
        path.write_text(json.dumps(bad))
        with pytest.raises(operator.OperatorError):
            operator.deploy_identity(str(path))
    with pytest.raises(operator.OperatorError):
        operator.deploy_identity('relative/contract.json')
