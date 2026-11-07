"""Manual published Python pin advancement and unchanged preview provenance."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_python_pins_retain_each_verified_published_source():
    manifest = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())
    sdk = manifest['components']['honua-sdk-python']
    assert sdk['version'] == '0.2.2'
    assert sdk['sha'] == 'ca6e228578e5db08e8a4dc5ec6b1dae690468874'
    # The customer install profile stays on its preserved rehearsal until a new cut rebinds it.
    install = json.loads((ROOT / 'customer-install-manifest.json').read_text())
    rehearsal = json.loads((ROOT / 'certification/customer-install/rehearsal-binding.v1.json')
                           .read_text())['clientArtifacts']
    for name, client in [('honua-sdk-python-wheel', 'honua-sdk'),
                         ('honua-admin-python-wheel', 'honua-admin')]:
        artifact = manifest['clientArtifacts'][name]
        assert artifact['publicationState'] == 'published'
        assert artifact['version'] == sdk['version']
        assert artifact['sourceSha'] == sdk['sha']
        for field in ('sourceSha', 'version', 'filename', 'digest'):
            assert install['clients'][client][field] == rehearsal[name][field]


def test_r39_previews_keep_their_source_pins():
    preview = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())['experimental']
    for name, sha in [('honua-mobile', '4a356a86cd4d056aa13f6cf8bc6855ab7670c2e6'),
                      ('honua-collect', '7eb948b5e80680e3a4f33c69c671804eebc370fb')]:
        assert preview[name]['sourcePinnedOnly'] is True
        assert preview[name]['sha'] == sha
