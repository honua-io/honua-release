"""Manual published Python pin advancement and unchanged preview provenance."""
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_python_pins_retain_each_verified_published_source():
    manifest = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())
    sdk = manifest['components']['honua-sdk-python']
    # 2026-10-26 rc hand rebind to the published 0.1.19 (python-sdk-v0.1.19).
    assert sdk['version'] == '0.1.19'
    assert sdk['sha'] == '4b5838cb5ba8c22a082cb2b7bdffc0c3fc1fd352'
    # The customer install profile stays on its preserved pre-cut rehearsal (owner decision 19)
    # until the rc.3 mint rebinds it; it must equal that binding, not the advanced platform pin.
    install = json.loads((ROOT / 'customer-install-manifest.json').read_text())
    rehearsal = json.loads((ROOT / 'certification/customer-install/rehearsal-binding.v1.json')
                           .read_text())['clientArtifacts']
    for name, client, version, source in [('honua-sdk-python-wheel', 'honua-sdk', '0.1.19', sdk['sha']),
                                          ('honua-admin-python-wheel', 'honua-admin', '0.1.12',
                                           'a2729be7b2fa0e363319043e9d084b2204d30a08')]:
        artifact = manifest['clientArtifacts'][name]
        assert artifact['publicationState'] == 'published'
        assert artifact['version'] == version
        assert artifact['sourceSha'] == source
        for field in ('sourceSha', 'version', 'filename', 'digest'):
            assert install['clients'][client][field] == rehearsal[name][field]


def test_r39_previews_keep_their_source_pins():
    preview = yaml.safe_load((ROOT / 'platform-manifest.yaml').read_text())['experimental']
    for name, sha in [('honua-mobile', '4a356a86cd4d056aa13f6cf8bc6855ab7670c2e6'),
                      ('honua-collect', '7eb948b5e80680e3a4f33c69c671804eebc370fb')]:
        assert preview[name]['sourcePinnedOnly'] is True
        assert preview[name]['sha'] == sha
