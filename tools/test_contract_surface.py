"""Self-test for the contract-surface gate — proves each verdict (pass/fail/blocked) can fire.

A gate that cannot fail is worse than no gate (AGENTS.md). These tests exercise the deterministic
extractors on sample source and the pure `check` decision on controlled surfaces.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import contract_surface as cs


def test_git_show_decodes_committed_source_as_utf8(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)
    source = '{"description":"wire-compatible — OGC §7.11"}\n'
    (tmp_path / "openapi.json").write_text(source, encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "openapi.json"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "fixture"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert cs._git_show(tmp_path, sha, "openapi.json") == source


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


# ── TypeScript entry-point surface (honua-release#104) ──────────────────────
# The gate's TS extractor used to key each symbol by its DECLARING FILE, so moving a declaration and
# re-exporting it from where it used to live read as a public-API removal — a breaking change that
# never happened. These tests pin the fixed behaviour: the surface is what a consumer can import from
# a published `exports` subpath.
def _ts_package(tmp_path: Path, exports: dict[str, str]) -> dict:
    return {"name": "@honua/fixture", "exports": {k: {"types": v} for k, v in exports.items()}}


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def test_ts_entry_point_surface_is_keyed_by_importable_subpath(tmp_path: Path) -> None:
    _write(tmp_path, "index.ts", 'export { Widget } from "./widget.js";\nexport const VERSION = "1";\n')
    _write(tmp_path, "widget.ts", "export class Widget {}\n")
    _write(tmp_path, "react/index.ts", "export function useHonua() {}\n")
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts", "./react": "./dist/src/react/index.d.ts"})

    entries, surface = cs._ts_entry_point_surface(tmp_path, pkg)

    assert entries == {".": "index.ts", "./react": "react/index.ts"}
    assert surface == sorted([".: value VERSION", ".: value Widget", "./react: value useHonua"])
    # the declaring file is deliberately absent from the key — it is not part of the import contract
    assert not any("widget.ts" in s for s in surface)


def test_ts_moved_and_reexported_symbol_is_not_reported_as_removed(tmp_path: Path) -> None:
    """honua-release#104: a pure file move that keeps the export is NOT a surface change."""
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts"})

    before_root = tmp_path / "before"
    _write(before_root, "index.ts", 'export { connect, validateConnectEndpoint } from "./connect.js";\n')
    _write(
        before_root,
        "connect.ts",
        "export function connect() {}\nexport function validateConnectEndpoint(u: string) { return u; }\n",
    )
    _entries_before, before = cs._ts_entry_point_surface(before_root, pkg)

    # the refactor under test: the declaration moves to its own module and connect.ts re-exports it.
    after_root = tmp_path / "after"
    _write(after_root, "index.ts", 'export { connect, validateConnectEndpoint } from "./connect.js";\n')
    _write(
        after_root,
        "connect.ts",
        'import { validateConnectEndpoint } from "./connect-endpoint.js";\n'
        'export { validateConnectEndpoint } from "./connect-endpoint.js";\n'
        "export function connect() { return validateConnectEndpoint(''); }\n",
    )
    _write(after_root, "connect-endpoint.ts",
           "export function validateConnectEndpoint(u: string) { return u; }\n")
    _entries_after, after = cs._ts_entry_point_surface(after_root, pkg)

    assert sorted(set(before) - set(after)) == [], "a moved-and-re-exported symbol must not read as removed"
    assert before == after


def test_ts_reexported_interface_as_type_only_is_not_a_removal(tmp_path: Path) -> None:
    """`interface X` relocated and re-exported via `export type { X }` is the same to a consumer."""
    pkg = _ts_package(tmp_path, {"./runtime": "./dist/src/runtime/index.d.ts"})

    before_root = tmp_path / "before"
    _write(before_root, "runtime/index.ts", 'export * from "./pmtiles-protocol.js";\n')
    _write(before_root, "runtime/pmtiles-protocol.ts", "export interface MaplibreProtocolRegistrar {}\n")
    _entries, before = cs._ts_entry_point_surface(before_root, pkg)

    after_root = tmp_path / "after"
    _write(after_root, "runtime/index.ts", 'export * from "./pmtiles-protocol.js";\n')
    _write(after_root, "runtime/pmtiles-protocol.ts",
           'export type { MaplibreProtocolRegistrar } from "./types.js";\n')
    _write(after_root, "runtime/types.ts", "export interface MaplibreProtocolRegistrar {}\n")
    _entries, after = cs._ts_entry_point_surface(after_root, pkg)

    assert before == after == ["./runtime: type MaplibreProtocolRegistrar"]


def test_ts_entry_point_surface_still_reports_a_real_removal(tmp_path: Path) -> None:
    """The fix must not blunt the gate: dropping a name from an entry point is still a removal."""
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts"})
    _write(tmp_path, "index.ts", 'export { a, b } from "./impl.js";\n')
    _write(tmp_path, "impl.ts", "export const a = 1;\nexport const b = 2;\n")
    _entries, before = cs._ts_entry_point_surface(tmp_path, pkg)

    _write(tmp_path, "index.ts", 'export { a } from "./impl.js";\n')  # b is no longer importable
    _entries, after = cs._ts_entry_point_surface(tmp_path, pkg)

    assert sorted(set(before) - set(after)) == [".: value b"]


def test_ts_type_only_export_is_distinguished_from_a_value(tmp_path: Path) -> None:
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts"})
    _write(tmp_path, "index.ts", "export type Options = { a: string };\nexport const DEFAULTS = {};\n")
    _entries, surface = cs._ts_entry_point_surface(tmp_path, pkg)
    assert surface == [".: type Options", ".: value DEFAULTS"]


def test_ts_unresolvable_star_export_is_recorded_not_dropped(tmp_path: Path) -> None:
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts"})
    _write(tmp_path, "index.ts", 'export * from "@honua/honua-migrate";\n')
    _entries, surface = cs._ts_entry_point_surface(tmp_path, pkg)
    assert surface == [".: star * from @honua/honua-migrate"]


def test_ts_import_cycle_terminates(tmp_path: Path) -> None:
    pkg = _ts_package(tmp_path, {".": "./dist/src/index.d.ts"})
    _write(tmp_path, "index.ts", 'export * from "./a.js";\n')
    _write(tmp_path, "a.ts", 'export * from "./b.js";\nexport const fromA = 1;\n')
    _write(tmp_path, "b.ts", 'export * from "./a.js";\nexport const fromB = 2;\n')
    _entries, surface = cs._ts_entry_point_surface(tmp_path, pkg)
    assert ".: value fromA" in surface and ".: value fromB" in surface


def test_ts_surface_falls_back_to_declaring_files_without_entry_points(tmp_path: Path) -> None:
    """No resolvable entry point must never mean "this package exports nothing"."""
    _write(tmp_path, "thing.ts", "export const Thing = 1;\n")
    entries, surface = cs._ts_entry_point_surface(tmp_path, {"name": "@honua/fixture"})
    assert entries == {} and surface == []
    # _extract_js turns that empty result into the declaring-file digest rather than certifying empty.
    fallback = cs._ts_export_surface(tmp_path)
    assert fallback == ["thing.ts: const Thing"]


def _git_fixture(root: Path, files: dict[str, str]) -> str:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "test@example.com"], check=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True)
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


def test_extract_js_reads_entry_points_from_the_committed_package_json(tmp_path: Path) -> None:
    import json

    sha = _git_fixture(tmp_path, {
        "package.json": '{"name":"@honua/fixture","exports":{".":{"types":"./dist/src/index.d.ts"}}}\n',
        "src/index.ts": 'export { Widget } from "./widget.js";\n',
        "src/widget.ts": "export class Widget {}\n",
    })
    payload = json.loads(cs._extract_js(tmp_path, sha)["public-api.json"])
    assert payload["mode"] == "entry-points"
    assert payload["entryPoints"] == ["."]
    assert payload["exports"] == [".: value Widget"]


def test_extract_js_falls_back_when_no_entry_point_resolves(tmp_path: Path) -> None:
    import json

    sha = _git_fixture(tmp_path, {"src/thing.ts": "export const Thing = 1;\n"})  # no package.json
    payload = json.loads(cs._extract_js(tmp_path, sha)["public-api.json"])
    assert payload["mode"] == "declaration-files"
    assert payload["exports"] == ["thing.ts: const Thing"]
