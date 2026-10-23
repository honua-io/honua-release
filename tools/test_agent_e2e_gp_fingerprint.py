"""Independent native-writer vectors and fail-closed request-shape controls."""
import copy
import json
from pathlib import Path

import pytest

from agent_e2e_gp_fingerprint import fingerprint, fingerprint_bytes
from agent_e2e_pack import PackError

VECTORS = json.loads((Path(__file__).parent/'fixtures'/'gp-native-fingerprints.json').read_text())['vectors']


@pytest.mark.parametrize('vector', VECTORS, ids=lambda row: row['name'])
def test_matches_actual_dotnet_native_writer(vector):
    assert fingerprint(vector['plan']) == vector['expectedFingerprint']


def test_hash_retains_step_order_but_sorts_native_sets_and_excludes_warnings():
    plan = copy.deepcopy(VECTORS[-1]['plan'])
    expected = fingerprint(plan)
    plan['outputs'].reverse()
    plan['steps'][0]['dependsOn'].reverse()
    plan['steps'][0]['inputs'] = dict(reversed(list(plan['steps'][0]['inputs'].items())))
    plan['warnings'] = ['different non-authoritative warning']
    assert fingerprint(plan) == expected
    plan['steps'].reverse()
    assert fingerprint(plan) != expected


@pytest.mark.parametrize('field,value', [('distance', '501'), ('input', 'another-shelter'), ('dissolve', 'true')])
def test_accepted_job_cannot_join_a_different_original_plan(field, value):
    plan = copy.deepcopy(VECTORS[1]['plan'])
    plan['steps'][0]['inputs'][field] = value
    assert fingerprint(plan) != VECTORS[1]['expectedFingerprint']


@pytest.mark.parametrize('plan', [None, [], {'steps':[None]}, {'steps':[{'kind':'Geoprocess','inputs':{'source':None}}]},
    {'steps':[{'kind':'Geoprocess','dependsOn':[None]}]}, {'warnings':[None]}, {'outputs':[None]},
    {'outputs':['42']}, {'outputs':['Map, File']}, {'steps':[{'kind':'999'}]}, {'steps':[{'kind':None}]},
    {'steps':[{'kind':'Geoprocess','rasterSources':{}}]}, {'workspaceId':'foreign'}, {'outputCrs':3857},
    {'steps':{'kind':'Geoprocess'}}, {'planId':123}, {'planId':'\ud800'}, {'steps':[{'kind':'Export','inputs':{'x':1}}]}])
def test_unreviewed_wire_fields_or_invalid_native_values_are_rejected(plan):
    with pytest.raises(PackError):
        fingerprint(plan)


def test_native_wire_normalization_preserves_empty_defaults_without_inventing_controls():
    assert fingerprint({}) == fingerprint({'planId':None,'intentId':None,'steps':None,'outputs':None,'warnings':None})
    assert b'workspace' not in fingerprint_bytes({})
    assert b'rasterSources' not in fingerprint_bytes({'steps':[{'kind':'Export'}]})
