#!/usr/bin/env python3
"""Record image digests observed from every running ECS task in this provisioned cell."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'e2e'))
import cloud_journey
import run_cloud


def observe(target, manifest, *, run=subprocess.run):
    """Reuse the ECS readback verifier and retain the actual returned platform digests."""
    descriptions = []
    def capture(command, **kwargs):
        result = run(command, **kwargs)
        if command[:3] == ['aws', 'ecs', 'describe-tasks']:
            descriptions.append(json.loads(result.stdout))
        return result
    image, detail = cloud_journey.observe_ecs_image(target, manifest, run=capture)
    if not image or not descriptions:
        raise ValueError('ECS task image verification failed: ' + str(detail))
    repository = cloud_journey._image_repository(manifest['components']['honua-server']['image'])
    tasks = descriptions[-1].get('tasks') or []
    digests = sorted({container.get('imageDigest') for task in tasks if task.get('lastStatus') == 'RUNNING'
                      for container in task.get('containers', [])
                      if cloud_journey._image_repository(str(container.get('image', ''))) == repository})
    if not digests or any(not isinstance(d, str) for d in digests):
        raise ValueError('ECS returned no usable server image digests')
    return {'reference': image, 'verified': True, 'source': 'ecs-task-readback', 'observedDigests': digests}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    target = run_cloud._target('aws-ecs')
    target._workdir = target._iac_root()
    result = observe(target, cloud_journey.manifest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, sort_keys=True) + '\n')

if __name__ == '__main__':
    main()
