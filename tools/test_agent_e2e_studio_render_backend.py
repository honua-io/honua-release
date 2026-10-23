"""Render identity joins replay genuine local MCP/HTTP save originals."""
import copy
from pathlib import Path
import subprocess
import sys

import pytest

from agent_e2e_pack import PackError
from agent_e2e_studio_render_backend import bind
from test_agent_e2e_native_authoring_bindings import saved, authoring, adapter, server
from test_agent_e2e_studio_render import rendering


def declaration(saved, rendering):
    template = copy.deepcopy(rendering[1])
    template['mapLibreModule'] = '/_studio/maplibre.js'
    template['runtimeAssets'] += [{'path': '/_studio/maplibre.js', 'sha256': 'd'*64},
                                  {'path': '/_studio/worker.js', 'sha256': 'e'*64}]
    relationships = saved['declaration']['relationships']
    template['candidateKey'] = saved['context']['componentBindingDigest']
    template['scope'] = {key: relationships[key] for key in ('itemId', 'namespace', 'ownerPrincipalId')}
    # This digest came from the desired body frozen before saved's model actions.
    template['scope']['documentSha256'] = saved['expected']['documentSha256']
    return {'schemaVersion': 1, 'savedVersionBinding': 'save', 'contract': template,
            'observer': {'profile': 'private-human-browser-not-configured'}}


def joined(value, saved):
    return bind(value, studio=saved['declaration']['relationships'], declarations={'save': saved['declaration']},
                reports={'save': saved['report']}, context=saved['context'], selection=saved['selected'])


def test_render_joins_only_original_native_draft_version_and_preserves_frozen_pixels(saved, rendering):
    value = declaration(saved, rendering)
    before = copy.deepcopy(value)
    actual = joined(value, saved)
    assert actual['scope']['versionId'] == saved['report']['values']['savedVersionId']
    assert actual['scope']['draftId'] == saved['selected']['resource']['sourceDraftId']
    actual['scope'].pop('draftId'); actual['scope'].pop('versionId')
    assert actual == before['contract'] and value == before


@pytest.mark.parametrize('change', ['expected-body', 'preselected-version', 'different-save', 'missing-transcript',
                                  'wrong-owner', 'missing-original', 'different-candidate'])
def test_render_cannot_select_unrelated_save_or_derive_expected_content(saved, rendering, change):
    value = declaration(saved, rendering)
    if change == 'expected-body': value['contract']['scope']['documentSha256'] = 'f'*64
    elif change == 'preselected-version': value['contract']['scope']['versionId'] = saved['selected']['resource']['versionId']
    elif change == 'different-save': saved['selected']['ordinal'] += 1
    elif change == 'missing-transcript': saved['context']['modelActions'] = []
    elif change == 'wrong-owner': value['contract']['scope']['ownerPrincipalId'] = 'different-owner'
    elif change == 'missing-original': saved['report']['records'].pop('saveHandle')
    elif change == 'different-candidate': value['contract']['candidateKey'] = 'sha256:'+'f'*64
    with pytest.raises((PackError, KeyError)): joined(value, saved)


def test_tools_script_entry_reaches_private_observer_without_repository_on_module_path(tmp_path):
    # Production launches `python tools/agent_e2e_campaign.py`, whereas pytest
    # also exposes the repository root. Keep that extra namespace unavailable.
    code = '''
import sys, types
sys.path.insert(0, sys.argv[1])
import agent_e2e_studio_render_backend as backend
backend.bind = lambda *args, **kwargs: {}
package = types.ModuleType('hosted_pilot')
package.__path__ = []
observer = types.ModuleType('hosted_pilot.browser_render')
def reached(**kwargs):
    raise RuntimeError('private-observer-reached')
observer.observe_original_render = reached
sys.modules['hosted_pilot'] = package
sys.modules['hosted_pilot.browser_render'] = observer
try:
    backend.observe({'savedVersionBinding':'save','observer':{}}, studio={},
        declarations={'save':{}}, reports={'save':{}}, context={}, selection={}, durable=None)
except RuntimeError as error:
    if str(error) != 'private-observer-reached': raise
else:
    raise AssertionError('private observer was not invoked')
'''
    result = subprocess.run([sys.executable, '-I', '-c', code, str(Path(__file__).resolve().parent)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
