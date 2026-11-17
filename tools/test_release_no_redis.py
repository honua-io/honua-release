"""The private Redis-on campaign cannot dispatch."""
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def test_private_redis_on_campaign_cannot_dispatch(tmp_path):
    guard = workflow('agent-e2e-campaign.yml')['jobs']['ecs']['steps'][0]
    assert guard['name'] == 'Require Redis-free private campaign binding'
    result = subprocess.run(['bash', '-c', guard['run']], cwd=tmp_path, capture_output=True)
    assert result.returncode != 0
    assert b'Redis-on qualification is retired' in result.stdout
