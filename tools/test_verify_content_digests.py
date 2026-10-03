"""The declared lock content digests must equal the bytes at their pinned source revisions.

These tests build a real git repository, commit real bytes, and compute the expected digest with
hashlib directly from those bytes - never from the verifier's own output. The offline reader uses
git objects, so a newer branch head or a dirty working tree cannot satisfy a pinned revision.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

import verify_content_digests as verifier

STANDARD = b'{"title": "geospatial-mcp JSON Schema index", "tools": []}\n'
EXPECTED = "sha256:" + hashlib.sha256(STANDARD).hexdigest()


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(("git", "-C", str(repository)) + arguments,
                            capture_output=True, check=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def source_root(tmp_path):
    repository = tmp_path / "honua-io" / "geospatial-mcp"
    (repository / "spec" / "schemas").mkdir(parents=True)
    _git(repository.parent, "init", "-q", "-b", "trunk", str(repository))
    (repository / "spec" / "schemas" / "index.json").write_bytes(STANDARD)
    _git(repository, "add", "-A")
    _git(repository, "-c", "user.name=t", "-c", "user.email=t@test", "commit", "-qm", "pin")
    revision = _git(repository, "rev-parse", "HEAD")
    # The working tree moves on after the pin; the pinned commit must still decide the verdict.
    (repository / "spec" / "schemas" / "index.json").write_bytes(b"{}\n")
    _git(repository, "add", "-A")
    _git(repository, "-c", "user.name=t", "-c", "user.email=t@test", "commit", "-qm", "later")
    return tmp_path, revision


def manifest_at(tmp_path, **digest) -> Path:
    path = tmp_path / "platform-manifest.yaml"
    path.write_text(yaml.safe_dump(
        {"platformRelease": "2026.1", "components": {},
         "platformLockEvidence": {"contentDigests": digest}}), encoding="utf-8")
    return path


def declaration(revision, **overrides):
    value = {"repository": "https://github.com/honua-io/geospatial-mcp", "revision": revision,
             "path": "spec/schemas/index.json", "sha256": EXPECTED}
    value.update(overrides)
    return value


def run(manifest: Path, root: Path) -> int:
    return verifier.main([str(manifest), "--source-root", str(root)])


def test_verifies_declared_digest_against_the_pinned_commit(source_root, capsys):
    root, revision = source_root
    assert run(manifest_at(root, geospatialMcp=declaration(revision)), root) == 0
    assert "geospatialMcp" in capsys.readouterr().out


def test_refuses_a_digest_that_does_not_match_the_pinned_bytes(source_root, capsys):
    root, revision = source_root
    manifest = manifest_at(root, geospatialMcp=declaration(revision, sha256="sha256:" + "0" * 64))
    assert run(manifest, root) == 1
    assert EXPECTED in capsys.readouterr().out


def test_refuses_the_head_of_a_branch_as_a_source_revision(source_root, capsys):
    root, _ = source_root
    manifest = manifest_at(root, geospatialMcp=declaration("trunk"))
    assert run(manifest, root) == 1
    assert "immutable 40-character git revision" in capsys.readouterr().out


def test_refuses_a_later_revision_of_the_same_file(source_root, capsys):
    """The digest is bound to bytes, not to a repository: a newer commit is a different fact."""
    root, _ = source_root
    head = _git(root / "honua-io" / "geospatial-mcp", "rev-parse", "HEAD")
    assert run(manifest_at(root, geospatialMcp=declaration(head)), root) == 1
    assert "hashes to" in capsys.readouterr().out


def test_refuses_a_content_digest_the_lock_cannot_carry(source_root, capsys):
    root, revision = source_root
    assert run(manifest_at(root, studio=declaration(revision)), root) == 1
    assert "no such content digest" in capsys.readouterr().out


def test_reports_an_undeclared_manifest_without_claiming_verification(source_root, capsys):
    root, _ = source_root
    assert run(manifest_at(root), root) == 0
    assert "declares no lock content digest" in capsys.readouterr().out


def test_repository_manifest_declares_verified_content_digests():
    """The committed manifest's declarations must at least be well formed and complete."""
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "platform-manifest.yaml").read_text(encoding="utf-8"))
    declared = verifier.declared_digests(manifest)
    assert declared["geospatialMcp"][0] == (
        "sha256:595f0ac8e1e129d4b78e1c4c40abfb71fc87d2d4bf5566a6bede311ed81583c5")
    assert declared["geospatialMcp"][1] == {
        "repository": "https://github.com/honua-io/geospatial-mcp",
        "revision": "d5a09d13c4ad541c05702e598c3679c0f42db7af",
        "path": "spec/schemas/index.json",
    }


def test_verifier_refuses_two_identities_for_one_standard(source_root, capsys):
    """Matching its own bytes is not enough when the manifest also names the standard elsewhere."""
    root, revision = source_root
    path = root / "platform-manifest.yaml"
    path.write_text(yaml.safe_dump({
        "platformRelease": "2026.1",
        "components": {"geospatial-mcp": {
            "repository": "https://github.com/honua-io/geospatial-mcp", "sha": revision,
            "artifact": f"spec:https://github.com/honua-io/geospatial-mcp/blob/{revision}/spec/schemas/index.json",
            "artifactSourceRevision": revision, "artifactSha256": "sha256:" + "1" * 64}},
        "platformLockEvidence": {"contentDigests": {"geospatialMcp": declaration(revision)}},
    }), encoding="utf-8")
    assert run(path, root) == 1
    assert "one standard has one identity" in capsys.readouterr().out


@pytest.fixture
def server_root(tmp_path):
    """A honua-server checkout whose selected commit carries the OKF and catalog files, then moves on."""
    import resolve_trunk_candidate as resolver

    repository = tmp_path / "honua-io" / "honua-server"
    _git(tmp_path, "init", "-q", "-b", "trunk", str(repository))
    files = {path: f'{{"bytes": "{name} at the selected sha"}}\n'.encode()
             for name, (_, path) in resolver.CONTENT_DIGEST_SOURCES.items()}

    def commit(contents, message):
        for path, raw in contents.items():
            (repository / path).parent.mkdir(parents=True, exist_ok=True)
            (repository / path).write_bytes(raw)
        _git(repository, "add", "-A")
        _git(repository, "-c", "user.name=t", "-c", "user.email=t@test", "commit", "-qm", message)
        return _git(repository, "rev-parse", "HEAD")

    selected = commit(files, "selected")
    later = commit({path: b"{}\n" for path in files}, "later")
    return tmp_path, selected, later, files


def resolved_digests(root, revision):
    """What the resolver declares when it selects `revision`, read through the offline git reader."""
    import resolve_trunk_candidate as resolver

    reader = verifier.SourceReader(root)

    class Contents:
        def file(self, repository, sha, path):
            return reader(f"https://github.com/{repository}", sha, path)

    candidate = {"components": {"honua-server": {
        "repository": "https://github.com/honua-io/honua-server", "sha": revision}}}
    return {name: resolver.content_digest_declaration(Contents(), candidate, name)
            for name in resolver.CONTENT_DIGEST_SOURCES}


def test_resolver_declarations_verify_against_the_selected_server_bytes(server_root, capsys):
    """#231 WI-7: what the resolver writes is exactly what this verifier re-reads and accepts."""
    root, selected, _, files = server_root
    digests = resolved_digests(root, selected)
    for declaration in digests.values():
        assert declaration["sha256"] == "sha256:" + hashlib.sha256(files[declaration["path"]]).hexdigest()
    assert run(manifest_at(root, **digests), root) == 0
    out = capsys.readouterr().out
    assert "okf (scripts/ci/okf-bundle.v1.json@" in out and "catalog (" in out


def test_a_resolver_declaration_does_not_verify_at_another_server_sha(server_root, capsys):
    root, selected, later, _ = server_root
    digests = resolved_digests(root, selected)
    digests["okf"]["revision"] = later
    assert run(manifest_at(root, **digests), root) == 1
    assert "contentDigests.okf" in capsys.readouterr().out
