"""Image admission uses measured bytes and the requested release profile."""
import argparse
import json

import pytest
import release_promise as p
from test_release_promise import aggregate, run, CANDIDATE, NOW


@pytest.mark.parametrize('size', [None, True, '220000000', 0, -1, 370_000_001])
def test_aot_receipt_requires_positive_integer_bytes_within_rc_budget(size):
    row = next(row for row in p.load_contract() if row['id'] == 'cloud-image-aot')
    record = run(row)
    record['reports'][0]['imageSizeBytes'] = size
    assert p.evaluate_row(row, [record], (CANDIDATE, NOW))['status'] == 'invalid'


def test_aot_receipt_preserves_measured_bytes_for_profile_admission():
    row = next(row for row in p.load_contract() if row['id'] == 'cloud-image-aot')
    record = run(row)
    record['reports'][0]['imageSizeBytes'] = 370_000_000
    result = p.evaluate_row(row, [record], (CANDIDATE, NOW))
    assert result['status'] == 'PASS'
    assert result['imageSizeBytes'] == 370_000_000


@pytest.mark.parametrize('case', [
    ('rc', 370_000_000, True), ('rc', 370_000_001, False),
    ('ga', 220_000_000, True), ('ga', 220_000_001, False),
    ('ga', 370_000_000, False), ('rc', None, False), ('rc', True, False),
])
def test_aggregate_enforces_requested_image_budget(case, monkeypatch):
    profile, size, expected = case
    monkeypatch.setattr(p.outcomes, 'report_valid', lambda *args: True)
    report = aggregate()
    next(row for row in report['rows'] if row['id'] == 'cloud-image-aot')['imageSizeBytes'] = size
    assert p.report_valid(report, profile, NOW) is expected


@pytest.mark.parametrize('case', [('rc', 0), ('ga', 1)])
def test_cli_applies_requested_image_budget(case, monkeypatch, tmp_path):
    profile, expected = case
    output = tmp_path / 'report.json'
    args = argparse.Namespace(server_sha=CANDIDATE.server_sha,
                              server_digest=CANDIDATE.server_digest,
                              platform_lock=CANDIDATE.platform_lock, profile=profile, out=str(output))
    row = {'id': 'cloud-image-aot', 'status': 'PASS', 'required': ['rc', 'ga'],
           'imageSizeBytes': 370_000_000, 'observedAt': '2026-11-14T00:00:00Z'}
    monkeypatch.setattr(p, 'arguments', lambda: (None, args))
    monkeypatch.setattr(p, 'collect', lambda *args: {})
    monkeypatch.setattr(p, 'evaluate', lambda *args: [row])
    monkeypatch.setattr(p.outcomes, 'report_valid', lambda *args: True)
    monkeypatch.setenv('GH_TOKEN', 'test-token')
    assert p.main() == expected
    assert json.loads(output.read_text())['status'] == ('pass' if expected == 0 else 'fail')
