"""Read the Studio provider region from the customer server container, never credentials."""
import json
import re
import shlex
import subprocess

from agent_e2e_local_recorder import ProjectionError, require


_INSPECT = r'''
import json, subprocess, sys
project, provider = sys.argv[1:]
query = subprocess.run(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=' + project,
                        '--filter', 'label=com.docker.compose.service=server'], capture_output=True, text=True)
ids = query.stdout.split()
if query.returncode or len(ids) != 1:
    sys.exit(1)
result = subprocess.run(['docker', 'inspect', ids[0]], capture_output=True, text=True)
if result.returncode:
    sys.exit(1)
row = json.loads(result.stdout)[0]
env = row['Config'].get('Env') or []
key = ('studioaiproxy__providers__' + provider + '__region').lower()
values = [item.partition('=')[2] for item in env if item.partition('=')[0].lower() == key]
args = (row['Config'].get('Entrypoint') or []) + (row['Config'].get('Cmd') or [])
if len(values) != 1 or any('studioaiproxy' in str(arg).lower() for arg in args):
    sys.exit(1)
print(json.dumps({'region': values[0], 'containerId': row['Id'], 'imageId': row['Image']}))
'''


def inspect_region(host, project, provider, *, run=subprocess.run):
    """Use a remote-side filter so secret container environment never leaves the host."""
    require(re.fullmatch(r'honua-eval-[1-4]', host or '') is not None
            and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', project or '') is not None
            and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', provider or '') is not None,
            'invalid provider inspection target')
    command = 'python3 -c ' + shlex.quote(_INSPECT) + ' ' + shlex.quote(project) + ' ' + shlex.quote(provider)
    result = run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', host, command],
                 stdin=subprocess.DEVNULL, capture_output=True, timeout=30, check=False)
    require(result.returncode == 0, 'server provider region could not be verified')
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise ProjectionError('server provider region evidence is malformed') from exc
    require(isinstance(data, dict) and set(data) == {'region', 'containerId', 'imageId'}
            and all(isinstance(data.get(k), str) for k in ('region', 'containerId', 'imageId'))
            and re.fullmatch(r'[a-z]{2}(?:-gov)?-[a-z]+-\d', data.get('region', ''))
            and re.fullmatch(r'[0-9a-f]{64}', data.get('containerId', ''))
            and re.fullmatch(r'sha256:[0-9a-f]{64}', data.get('imageId', '')),
            'server provider region evidence is malformed')
    return data


def retain_region(recorder, host, project, provider, *, run=subprocess.run):
    data = inspect_region(host, project, provider, run=run)
    recorder.store_studio_provider_config(data)
    return data
