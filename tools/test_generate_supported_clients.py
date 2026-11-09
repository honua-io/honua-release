import pytest

from generate_supported_clients import main, update_table


def test_release_notes_supported_clients_match_manifest():
    assert main(["--check"]) == 0


def test_table_uses_exact_published_artifacts_and_preserves_surrounding_prose():
    artifacts = {
        key: {"package": package, "version": version}
        for key, package, version in (
            ("honua-sdk-js", "@honua/sdk-js", "0.3.2"),
            ("honua-sdk-client-compat", "@honua/sdk-client-compat", "0.3.2"),
            ("honua-sdk-python-wheel", "honua-sdk", "0.2.2"),
            ("honua-sdk-dotnet", "Honua.Sdk", "2.1.0"),
        )
    }
    narrative = "Before\n\n### Supported clients\n\n| SDK | Old |\n|---|---|\n| stale | pin |\n\nAfter\n"
    result = update_table(narrative, {"clientArtifacts": artifacts})
    assert result.startswith("Before\n\n### Supported clients\n\n")
    assert result.endswith("\n\nAfter\n")
    assert "stale" not in result
    for artifact in artifacts.values():
        assert f"`{artifact['package']}`" in result
        assert f"| {artifact['version']} |" in result
    assert update_table(result, {"clientArtifacts": artifacts}) == result


@pytest.mark.parametrize("narrative", ["No table", "### Supported clients\n\nMissing table\n"])
def test_missing_table_is_refused(narrative):
    with pytest.raises(ValueError, match="Supported clients table"):
        update_table(narrative, {"clientArtifacts": {}})
