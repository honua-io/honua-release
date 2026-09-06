from __future__ import annotations

import copy

import pytest
import yaml

from generate_compatibility_table import main, render
from sdk_baselines import SDK_COMPONENTS, check_component, content_digest, derive, findings
from test_platform_lock import DIGEST, REVISION, component, valid_lock
from validate_platform_lock import validate


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


def test_complete_lock_validator_requires_official_sdk_baseline():
    lock = valid_lock()
    lock["components"]["honua-sdk-js"] = lock["components"].pop("sdk")
    assert any("serverCompatibility" in error for error in validate(lock).errors)
    lock["components"]["honua-sdk-js"]["serverCompatibility"] = component()["serverCompatibility"]
    assert validate(lock).ok
    lock["components"]["honua-sdk-js"]["serverCompatibility"]["minimumServerVersion"] = "0.1.0"
    assert any("floor" in error for error in validate(lock).errors)


def test_table_is_deterministic_and_does_not_imply_upgrade_support():
    lock = valid_lock()
    del lock["components"]["honua-sdk-python"]
    output = render(lock)
    assert output == render(copy.deepcopy(lock))
    assert "## UPGRADE EDGES" in output
    assert "No qualified edge is pinned" in output
    assert "restoring a verified pre-upgrade backup is required" in output
    assert "| honua-sdk-python | source pin only | unqualified |" in output


def test_cli_documentation_check_cannot_be_confused_with_qualification(tmp_path):
    source, output = tmp_path / "lock.yaml", tmp_path / "table.md"
    lock = valid_lock()
    del lock["components"]["honua-sdk-python"]
    source.write_text(yaml.safe_dump(lock), encoding="utf-8")
    args = [str(source), "--output", str(output)]
    assert main(args) == 0
    assert main([*args, "--check-output"]) == 0
    assert main([*args, "--check"]) == 1
    output.write_text("stale", encoding="utf-8")
    assert main([*args, "--check-output"]) == 1


@pytest.mark.parametrize("name", SDK_COMPONENTS)
def test_nonempty_lock_cannot_omit_official_sdk(name):
    lock = valid_lock()
    del lock["components"][name]
    assert any(f"$.components.{name}: required official SDK" in error for error in validate(lock).errors)


def test_published_declaration_cannot_bind_newer_component_head():
    item = component()
    item["artifacts"] = [{"sourceRevision": "b" * 40}]
    with pytest.raises(ValueError, match="source"):
        check_component(item)
    item["serverCompatibility"]["declarations"][0]["revision"] = "b" * 40
    assert check_component(item) == "1.2.0"


def test_declarations_must_cover_all_shipped_artifact_revisions():
    item = component()
    item["artifacts"] = [{"sourceRevision": REVISION}, {"sourceRevision": "b" * 40}]
    with pytest.raises(ValueError, match="every artifact source revision"):
        check_component(item)
    declaration = copy.deepcopy(item["serverCompatibility"]["declarations"][0])
    declaration["revision"] = "b" * 40
    item["serverCompatibility"]["declarations"].append(declaration)
    assert check_component(item) == "1.2.0"
