"""Manual published Python pin advancement and unchanged preview provenance."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_python_pins_share_the_verified_published_source():
    manifest = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())
    sdk = manifest['components']['honua-sdk-python']
    assert sdk['version'] == '0.1.13'
    assert sdk['sha'] == 'a9cd320a1d330e758e1dd6c186f62a922d259cef'
    install = json.loads((ROOT / 'customer-install-manifest.json').read_text())
    for name, client, version in [('honua-sdk-python-wheel', 'honua-sdk', '0.1.13'),
                                  ('honua-admin-python-wheel', 'honua-admin', '0.1.10')]:
        artifact = manifest['clientArtifacts'][name]
        assert artifact['publicationState'] == 'published'
        assert artifact['version'] == version
        assert artifact['sourceSha'] == sdk['sha']
        for field in ('sourceSha', 'version', 'filename', 'digest'):
            assert install['clients'][client][field] == artifact[field]


def test_r39_previews_keep_their_source_pins():
    preview = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())['experimental']
    for name, sha in [('honua-mobile', '4a356a86cd4d056aa13f6cf8bc6855ab7670c2e6'),
                      ('honua-collect', '7eb948b5e80680e3a4f33c69c671804eebc370fb')]:
        assert preview[name]['sourcePinnedOnly'] is True
        assert preview[name]['sha'] == sha
