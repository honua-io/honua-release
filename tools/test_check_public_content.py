import json
from pathlib import Path
import subprocess

import pytest

import check_public_content


def repository(root, paths):
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    policy = root / ".github/hygiene.json"
    policy.parent.mkdir()
    source = Path(__file__).resolve().parents[1] / ".github/hygiene.json"
    policy.write_text(source.read_text())
    for relative in paths:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)


@pytest.mark.parametrize("path", [
    "tools/vendor-terms/reports/new-report.md",
    "docs/2026.1-lambda-ga-bill.md",
    "docs/rehearsals/2026-09-12-licensing-disabled.md",
])
def test_rejects_reintroduced_internal_content(tmp_path, path, capsys):
    repository(tmp_path, [path])
    assert check_public_content.main(tmp_path) == 1
    assert path in capsys.readouterr().out


def test_preserves_release_contracts_workflows_and_synthetic_fixtures(tmp_path):
    repository(tmp_path, ["contracts/promise.json", "release-notes/2026.1.md",
                          ".github/workflows/publish.yml", "tools/fixtures/synthetic.json"])
    assert check_public_content.main(tmp_path) == 0


@pytest.mark.parametrize("policy", [{}, {"denied_public_paths": []},
                                     {"denied_public_paths": [""]}])
def test_missing_or_malformed_policy_fails_closed(tmp_path, policy):
    repository(tmp_path, [])
    (tmp_path / ".github/hygiene.json").write_text(json.dumps(policy))
    assert check_public_content.main(tmp_path) == 1
