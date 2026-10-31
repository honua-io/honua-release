from pathlib import Path

import pytest

from real_map import prepare_map


def test_map_setup_refuses_same_credential_before_any_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv('OPS_MAP_CONTROLLER_TEST', 'same-private-key')
    monkeypatch.setenv('OPS_MAP_OPERATOR_TEST', 'same-private-key')
    with pytest.raises(ValueError, match='invalid map setup inputs'):
        prepare_map(project='ops-' + 'a' * 32, compose_file=Path('/missing'),
                    image_ref='server@sha256:' + 'b' * 64, base_url='http://127.0.0.1:1',
                    work=tmp_path / 'map', service='parcels', layer_id=0,
                    controller_env='OPS_MAP_CONTROLLER_TEST', operator_env='OPS_MAP_OPERATOR_TEST',
                    operator_id='11111111-1111-4111-8111-111111111111')
    assert not (tmp_path / 'map').exists()


def test_source_comparison_checks_features_and_receipt_integrity():
    import base64
    import hashlib
    import json
    from real_map import same_public_source
    def receipt(features):
        raw = json.dumps({'type': 'FeatureCollection', 'features': features}).encode()
        return {'path': '/source', 'status': 200, 'feature_count': len(features),
                'sha256': hashlib.sha256(raw).hexdigest(), 'body_b64': base64.b64encode(raw).decode()}
    one = {'type': 'Feature', 'properties': {'name': 'a'}, 'geometry': {'type': 'Point', 'coordinates': [0, 0]}}
    two = {'type': 'Feature', 'properties': {'name': 'b'}, 'geometry': {'type': 'Point', 'coordinates': [1, 1]}}
    original = receipt([one, two])
    assert same_public_source(original, receipt([two, one]))
    assert not same_public_source(original, receipt([one, {**two, 'properties': {'name': 'changed'}}]))
    corrupt = receipt([one, two])
    corrupt['sha256'] = '0' * 64
    assert not same_public_source(original, corrupt)


def test_map_source_refuses_nonlocal_origin_before_network():
    from real_map import observe_public_source
    with pytest.raises(ValueError, match='loopback'):
        observe_public_source('http://example.com:80', 'parcels', 0)
