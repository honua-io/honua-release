"""CLI-arm driver extension: controller-only datasource/upload fixture preparation.

This is fixture setup, never a model-selected action. Every journey oracle still
runs in the original live driver. Cloud provisioning remains external.
"""
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'certification/terminal-journey'))
import live_driver as driver


def prepare_fixture(request):
    state, target, _manifest, observation, _workspace = driver._rehydrate(request)
    if not state.get('stackUp') or not state.get('executionEnabled'):
        raise ValueError('fixture needs a verified running deployment')
    engine = driver._executor(state, target, observation)
    created = engine.sdk_call('CreateConnectionAsync', [engine.datasource_arguments()])
    tested = engine.sdk_call('TestConnectionAsync', [created['connectionId']])
    check = engine._check(3, 'datasource', 'TestConnectionAsync', lambda: engine.prove_connection(tested))
    if check.status != 'pass': raise ValueError('fixture datasource did not verify')
    engine.upload_dataset()
    driver._write_state(state['workspaceId'], state)
    return {'status': 'pass', 'attribution': 'harness-fixture', 'connectionCheck': check.as_receipt()}


def main():
    request = json.load(sys.stdin)
    if request.get('operation') == 'prepare_cli_fixture':
        if request.get('protocol') != driver.PROTOCOL: raise ValueError('invalid driver protocol')
        try:
            response = prepare_fixture(request)
        except Exception as exc:
            response = {'status': 'fail', 'errorCategory': type(exc).__name__}
    else:
        response = driver.handle(request)
    print(json.dumps(response))
    # Structured failures reach the adapter as responses, matching live_driver's
    # recoverable execute failures. Their status remains fail in the action log.
    return 0


if __name__ == '__main__': raise SystemExit(main())
