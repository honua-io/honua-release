"""Self-test for the contract-surface gate — proves each verdict (pass/fail/blocked) can fire.

A gate that cannot fail is worse than no gate (AGENTS.md). These tests exercise the deterministic
extractors on sample source and the pure `check` decision on controlled surfaces.
"""
from __future__ import annotations

from pathlib import Path

import contract_surface as cs


# ── syntactic extractors detect real surface changes ────────────────────────
def test_cs_surface_detects_added_and_removed(tmp_path: Path) -> None:
    (tmp_path / "a.cs").write_text(
        "namespace N;\npublic class Foo\n{\n    public int Bar { get; }\n}\n", encoding="utf-8"
    )
    before = cs._cs_public_surface(tmp_path)
    assert any("Foo" in s for s in before)
    assert any("Bar" in s for s in before)

    (tmp_path / "a.cs").write_text(
        "namespace N;\npublic class Foo\n{\n    public int Bar { get; }\n    public int Baz { get; }\n}\n",
        encoding="utf-8",
    )
    after = cs._cs_public_surface(tmp_path)
    assert set(after) - set(before), "adding a public member must change the surface"


def test_cs_surface_excludes_tests(tmp_path: Path) -> None:
    d = tmp_path / "Honua.Sdk.Tests"
    d.mkdir()
    (d / "t.cs").write_text("namespace N;\npublic class ShouldBeIgnored {}\n", encoding="utf-8")
    assert not any("ShouldBeIgnored" in s for s in cs._cs_public_surface(tmp_path))


def test_ts_surface_detects_exports(tmp_path: Path) -> None:
    (tmp_path / "index.ts").write_text(
        "export class Widget {}\nexport const VERSION = '1';\nexport { helper as helpAlias };\n"
        "export * from './other';\n",
        encoding="utf-8",
    )
    surface = cs._ts_export_surface(tmp_path)
    assert any("class Widget" in s for s in surface)
    assert any("const VERSION" in s for s in surface)
    assert any("helpAlias" in s for s in surface)
    assert any("* " in s and "./other" in s for s in surface)


def test_ts_surface_excludes_tests(tmp_path: Path) -> None:
    (tmp_path / "thing.test.ts").write_text("export class Nope {}\n", encoding="utf-8")
    assert not any("Nope" in s for s in cs._ts_export_surface(tmp_path))


# ── pure check() decision: pass / fail / blocked ────────────────────────────
def _manifest(tmp: Path) -> Path:
    p = tmp / "platform-manifest.yaml"
    p.write_text(
        "platformRelease: 2026.1-rc.0\ncomponents:\n"
        + "".join(f"  {c}:\n    sha: deadbeef{i}\n" for i, c in enumerate(cs.SURFACE_COMPONENTS)),
        encoding="utf-8",
    )
    return p


def _seed_baseline(base: Path, surfaces: dict[str, dict[str, str]]) -> None:
    for comp, arts in surfaces.items():
        d = base / comp
        d.mkdir(parents=True)
        for name, text in arts.items():
            (d / name).write_text(text, encoding="utf-8")


def _stub_extractors(monkeypatch, surfaces: dict[str, dict[str, str]]) -> None:
    monkeypatch.setattr(
        cs, "EXTRACTORS",
        {c: (lambda repo, sha, _c=c: surfaces[_c]) for c in cs.SURFACE_COMPONENTS},
    )


def test_check_blocked_without_baseline(tmp_path: Path) -> None:
    status, why, rows = cs.check("2026.1", tmp_path, _manifest(tmp_path), tmp_path / "baselines")
    assert status == "blocked"
    assert "baseline" in why.lower()


def test_check_pass_when_surfaces_match(tmp_path: Path, monkeypatch) -> None:
    surfaces = {c: {"public-api.json": f"{{{c}}}\n"} for c in cs.SURFACE_COMPONENTS}
    _stub_extractors(monkeypatch, surfaces)
    base = tmp_path / "baselines" / "2026.1"
    _seed_baseline(base, surfaces)
    status, _why, rows = cs.check("2026.1", tmp_path, _manifest(tmp_path), tmp_path / "baselines")
    assert status == "pass", rows


def test_check_fails_on_drift(tmp_path: Path, monkeypatch) -> None:
    surfaces = {c: {"public-api.json": f"{{{c}}}\n"} for c in cs.SURFACE_COMPONENTS}
    _stub_extractors(monkeypatch, surfaces)
    base = tmp_path / "baselines" / "2026.1"
    _seed_baseline(base, surfaces)
    # baseline for one component now differs from what the extractor returns -> drift.
    drift = base / "honua-sdk-dotnet" / "public-api.json"
    drift.write_text('{"stale":true}\n', encoding="utf-8")
    status, why, _rows = cs.check("2026.1", tmp_path, _manifest(tmp_path), tmp_path / "baselines")
    assert status == "fail"
    assert "honua-sdk-dotnet" in why


def test_check_fails_on_added_artifact(tmp_path: Path, monkeypatch) -> None:
    surfaces = {c: {"public-api.json": f"{{{c}}}\n"} for c in cs.SURFACE_COMPONENTS}
    surfaces["honua-server"]["extra.json"] = "{}\n"  # extractor sees a new artifact
    _stub_extractors(monkeypatch, surfaces)
    base = tmp_path / "baselines" / "2026.1"
    _seed_baseline(base, {c: {"public-api.json": f"{{{c}}}\n"} for c in cs.SURFACE_COMPONENTS})
    status, why, _rows = cs.check("2026.1", tmp_path, _manifest(tmp_path), tmp_path / "baselines")
    assert status == "fail"
    assert "honua-server" in why
