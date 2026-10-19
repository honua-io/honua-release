import copy
import json
from pathlib import Path

import pytest

from protocol_producer_context import context

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / 'certification/protocol-certification-requirements.v1.json').read_text())
PRODUCER = next(row for row in CATALOG['production']['producers'] if row['producer'] == 'honua-server-cite')
PIN = CATALOG['source_revisions']['server-certification']['commit']
CANDIDATE = {'server_sha': PIN, 'image_digest': 'sha256:' + 'b' * 64, 'cut_at': '2026-10-13T00:00:00Z'}


def test_context_preserves_exact_governed_fields_and_candidate():
    value = json.loads(context(CATALOG, PRODUCER, CANDIDATE, PIN))
    assert value['requirements_revision'] == CATALOG['revision']
    assert value['producer_source_sha'] == PIN
    assert value['candidate'] == {'source_sha': PIN, 'image_digest': CANDIDATE['image_digest'], 'cut_at': CANDIDATE['cut_at']}
    assert len(value['requirements']) == PRODUCER['cells']
    for row in value['requirements']:
        original = next(r for r in CATALOG['requirements'] if all(r[k] == row[k] for k in ('surface', 'operation', 'canonical_client', 'client_version', 'deployment_target')))
        assert row == {key: original[key] for key in row}


@pytest.mark.parametrize('mutation', ['duplicate', 'missing', 'wrong-workflow', 'unknown-tier', 'oversize'])
def test_context_refuses_invalid_denominator_before_dispatch(mutation):
    catalog, producer = copy.deepcopy(CATALOG), copy.deepcopy(PRODUCER)
    row = next(r for r in catalog['requirements'] if r['client_lane'] in producer['client_lanes'])
    if mutation == 'duplicate':
        catalog['requirements'].append(copy.deepcopy(row))
    elif mutation == 'missing':
        catalog['requirements'].remove(row)
    elif mutation == 'wrong-workflow':
        producer['workflow'] = 'unreviewed.yml'
    elif mutation == 'unknown-tier':
        row['required_tier'] = 'optional'
    else:
        row['contract_revision'] = 'x' * 60001
    with pytest.raises(ValueError):
        context(catalog, producer, CANDIDATE, PIN)


@pytest.mark.parametrize('revision', ['', 'with space', '../leading', 'é', 'x' * 201])
def test_revision_grammar_matches_producer_admission(revision):
    catalog = copy.deepcopy(CATALOG)
    catalog['revision'] = revision
    with pytest.raises(ValueError, match='catalog revision'):
        context(catalog, PRODUCER, CANDIDATE, PIN)


def test_sdk_context_does_not_invent_test_ids_or_include_other_lanes():
    catalog = copy.deepcopy(CATALOG)
    rows = [r for r in catalog['requirements'] if r['client_lane'] == 'sdk-js-certification']
    producer = {'producer': 'honua-sdk-js', 'repository': 'honua-io/honua-sdk-js', 'workflow': 'integration.yml',
                'client_lanes': ['sdk-js-certification'], 'cells': len(rows)}
    pin = catalog['source_revisions']['sdk-js']['commit']
    value = json.loads(context(catalog, producer, CANDIDATE, pin))
    assert len(value['requirements']) == len(rows)
    for original, actual in zip(rows, value['requirements']):
        assert ('test_ids' in actual) == ('test_ids' in original)
        assert actual['client_lane'] == 'sdk-js-certification'


def test_cite_fixture_mapping_is_closed_and_preserves_unrelated_catalog_fields(tmp_path):
    import runpy
    import shutil
    root = tmp_path / 'repo'
    shutil.copytree(ROOT / 'certification', root / 'certification')
    shutil.copy(ROOT / 'platform-manifest.yaml', root)
    namespace = runpy.run_path(str(root / 'certification/generate-protocol-requirements.py'), run_name='fixture_controls')
    expected = {
        'OGC API Features 1.0': 'docker/cite/ogc-api-features/seed.sql',
        'OGC API Tiles 1.0': 'docker/cite/ogc-api-tiles/seed.sql',
        'WFS 1.0': 'docker/cite/shared/test-data', 'WFS 1.1': 'docker/cite/shared/test-data',
        'WFS 2.0': 'docker/cite/shared/test-data', 'WCS 2.0': 'docker/cite/wcs20/seed.sql',
        'WMS 1.3': 'docker/cite/shared/seed/mapserver.sql', 'WMTS 1.0': 'docker/cite/shared/seed/mapserver.sql',
        'WPS 2.0': 'docker/cite/wps20/crs84-echo.xml',
    }
    assert namespace['CITE_FIXTURES'] == expected
    # Execute the actual generator. Only the eight existing mapped CITE rows
    # change fixture identity; this historical source contains no WPS row.
    import subprocess
    import sys
    result = subprocess.run([sys.executable, str(root / 'certification/generate-protocol-requirements.py')], cwd=root, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    actual = json.loads((root / 'certification/protocol-certification-requirements.v1.json').read_text())
    # Reconstruct the old generic CNG fixture binding in a controlled baseline,
    # so the regression remains meaningful after canonical regeneration.
    prior = copy.deepcopy(CATALOG['requirements'])
    for row in prior:
        if row['canonical_client'] == 'OGC CITE' and row['client_version'] in expected:
            row['fixture_revision'] = 'docker/cng/seed.sql@{source_sha}'
    changed = []
    assert len(actual['requirements']) == len(prior)
    for before, after in zip(prior, actual['requirements']):
        if before != after:
            assert before['canonical_client'] == 'OGC CITE'
            assert before['client_version'] in expected
            assert after['fixture_revision'] == expected[before['client_version']] + '@{source_sha}'
            assert {k: v for k, v in after.items() if k != 'fixture_revision'} == {k: v for k, v in before.items() if k != 'fixture_revision'}
            changed.append(before['surface'])
    assert len(changed) == 8
