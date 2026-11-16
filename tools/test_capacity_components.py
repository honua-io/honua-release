"""Synthetic evidence for current and historical capacity component contracts."""
import copy
import json

import pytest

import check_capacity_soak as gate
from digests import file_sha256_hex
from test_check_capacity_soak import (
    CURRENT_LOCK_PATH, IMAGE_DIGEST, LOCK, LOCK_PATH, REVISION, _recover_after,
    _rewrite_source, _set_drill, receipt,
)


def database_lock():
    return json.loads(CURRENT_LOCK_PATH.read_text())


def database_receipt(root):
    value = receipt(root)
    value['lockSha256'] = file_sha256_hex(CURRENT_LOCK_PATH)
    del value['topology']['redis']
    value['faultDrill']['recoveries'] = [
        event for event in value['faultDrill']['recoveries'] if event['dependency'] != 'redis'
    ]
    for signal in value['signals'].values():
        signal['topology'] = copy.deepcopy(value['topology'])
    saturation = value['signals']['saturationRatio']
    del saturation['saturationComponents']['redis']
    saturation['observationPopulation']['sampleCount'] = 122
    saturation['query'] = database_lock()['queries']['saturationRatio']
    recovery = value['signals']['recoveryTimeSeconds']
    recovery['observationPopulation']['sampleCount'] = 2
    recovery['recoveryEvidence']['events'] = copy.deepcopy(value['faultDrill']['recoveries'])

    def update(source):
        source['lockSha256'] = value['lockSha256']
        source['topology'] = copy.deepcopy(value['topology'])
        source['faultDrill'] = copy.deepcopy(value['faultDrill'])
        source['recoveries'] = copy.deepcopy(value['faultDrill']['recoveries'])
        for row in source['metrics']:
            del row['redis']
    _rewrite_source(value, root, update)
    return value


def evaluate(lock, value, root):
    path = CURRENT_LOCK_PATH if 'capacityComponents' in lock else LOCK_PATH
    return gate.evaluate(lock, value, file_sha256_hex(path), REVISION, root, IMAGE_DIGEST)


def test_database_only_capacity_evidence_passes(tmp_path):
    assert evaluate(database_lock(), database_receipt(tmp_path), tmp_path) == []


def test_historical_lock_still_requires_redis(tmp_path):
    assert evaluate(LOCK, receipt(tmp_path), tmp_path) == []
    assert evaluate(LOCK, database_receipt(tmp_path), tmp_path)


@pytest.mark.parametrize('components', [[], ['worker'], ['database'], ['worker', 'database', 'other'],
                                        ['worker', 'database', 'database'], None])
def test_unknown_or_incomplete_component_policy_fails(tmp_path, components):
    lock = database_lock()
    lock['capacityComponents'] = components
    assert evaluate(lock, database_receipt(tmp_path), tmp_path)


@pytest.mark.parametrize('dependency', ['worker', 'database'])
def test_current_contract_requires_every_fault(tmp_path, dependency):
    value = database_receipt(tmp_path)
    _set_drill(value, tmp_path, lambda drill: drill.update(recoveries=[
        event for event in drill['recoveries'] if event['dependency'] != dependency
    ]))
    assert any(f'{dependency} fault did not recover' in failure
               for failure in evaluate(database_lock(), value, tmp_path))


@pytest.mark.parametrize('dependency,seconds', [('worker', 20), ('database', 37)])
def test_current_contract_enforces_recovery_deadlines(tmp_path, dependency, seconds):
    value = database_receipt(tmp_path)
    _recover_after(value, tmp_path, dependency, seconds)
    assert any(f'fault drill: {dependency} recovered in {seconds}s, beyond' in failure
               for failure in evaluate(database_lock(), value, tmp_path))


@pytest.mark.parametrize('mutation', ['database', 'population', 'integrity', 'jobs', 'binding'])
def test_current_contract_keeps_evidence_controls(tmp_path, mutation):
    value = database_receipt(tmp_path)
    if mutation == 'database':
        del value['topology']['database']
    elif mutation == 'population':
        value['signals']['saturationRatio']['observationPopulation']['sampleCount'] = 183
    elif mutation == 'integrity':
        value['faultDrill']['dataIntegrity']['payloadSha256'] = 'f' * 64
    elif mutation == 'jobs':
        value['faultDrill']['gpJobs']['stuckJobIds'] = ['unfinished']
    else:
        value['lockSha256'] = '0' * 64
    assert evaluate(database_lock(), value, tmp_path)
