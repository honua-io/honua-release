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
