from __future__ import annotations

import copy

import pytest
import yaml

from generate_compatibility_table import main, render
from sdk_baselines import check_component, content_digest, derive, findings
from test_platform_lock import DIGEST, REVISION, valid_lock


def component():
    evidence = {"uri": "https://example.test/introduction", "sha256": DIGEST}
    content = {"capabilities": {
        "admin.read": {"minimumServerVersion": "1.0.0", "versionModel": "semver", "evidence": evidence},
        "admin.write": {"minimumServerVersion": "1.2.0", "versionModel": "semver", "evidence": evidence},
        "optional": {"minimumServerVersion": "9.0.0", "versionModel": "semver", "evidence": evidence},
    }}
    return {
        "source": {"revision": REVISION},
        "artifacts": [],
        "serverCompatibility": {
            "minimumServerVersion": "1.2.0",
            "manifests": [{
                "source": {"repository": "https://github.com/honua-io/honua-server", "revision": REVISION, "path": "capabilities.json"},
                "content": content, "sha256": content_digest(content),
                "requiredCapabilities": ["admin.read", "admin.write"],
            }],
            "declarations": [{"revision": REVISION, "path": "compatibility.json", "sha256": DIGEST, "minimumServerVersion": "1.2.0"}],
        },
    }


def test_maximum_required_floor_excludes_optional_capabilities():
    assert check_component(component()) == "1.2.0"


def test_multiple_manifests_include_every_consumed_requirement():
    item = component()
    other = copy.deepcopy(item["serverCompatibility"]["manifests"][0])
    other["requiredCapabilities"] = ["optional"]
    item["serverCompatibility"]["manifests"].append(other)
    assert derive(item["serverCompatibility"]) == "9.0.0"


def test_rejects_tampered_manifest_content():
    item = component()
    item["serverCompatibility"]["manifests"][0]["content"]["capabilities"]["admin.write"]["minimumServerVersion"] = "0.1.0"
    with pytest.raises(ValueError, match="digest"):
        check_component(item)


@pytest.mark.parametrize("field", ["minimumServerVersion", "versionModel", "evidence"])
def test_missing_capability_introduction_is_unqualified(field):
    item = component()
    manifest = item["serverCompatibility"]["manifests"][0]
    del manifest["content"]["capabilities"]["admin.read"][field]
    manifest["sha256"] = content_digest(manifest["content"])
    with pytest.raises(ValueError, match="unqualified"):
        check_component(item)


def test_undeclared_required_capability_is_not_silently_ignored():
    item = component()
    item["serverCompatibility"]["manifests"][0]["requiredCapabilities"].append("absent")
    with pytest.raises(ValueError, match="absent"):
        check_component(item)


@pytest.mark.parametrize("location", ["lock", "declaration"])
def test_rejects_conflicting_baseline(location):
    item = component()
    target = item["serverCompatibility"] if location == "lock" else item["serverCompatibility"]["declarations"][0]
    target["minimumServerVersion"] = "0.1.0"
    with pytest.raises(ValueError, match="floor"):
        check_component(item)


def test_declaration_must_bind_source():
    item = component()
    item["serverCompatibility"]["declarations"][0]["revision"] = "b" * 40
    with pytest.raises(ValueError, match="source"):
        check_component(item)


def test_empty_lock_cannot_pass_by_omitting_sdk_roster():
    assert len(findings({})) == 4


def test_table_is_deterministic_and_does_not_imply_upgrade_support():
    lock = valid_lock()
    output = render(lock)
    assert output == render(copy.deepcopy(lock))
    assert "## UPGRADE EDGES" in output
    assert "No qualified edge is pinned" in output
    assert "restoring a verified pre-upgrade backup is required" in output
    assert "| honua-sdk-python | source pin only | unqualified |" in output


def test_cli_documentation_check_cannot_be_confused_with_qualification(tmp_path):
    source, output = tmp_path / "lock.yaml", tmp_path / "table.md"
    source.write_text(yaml.safe_dump(valid_lock()), encoding="utf-8")
    args = [str(source), "--output", str(output)]
    assert main(args) == 0
    assert main([*args, "--check-output"]) == 0
    assert main([*args, "--check"]) == 1
    output.write_text("stale", encoding="utf-8")
    assert main([*args, "--check-output"]) == 1
