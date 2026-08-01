#!/usr/bin/env python3
"""Contract-surface gate (gate b, `contract-rest-sdk`) — REST/OpenAPI + SDK public-API drift.

The proto half of the contract gate (`buf breaking`) already runs against geospatial-grpc's release
tags. The REST/OpenAPI + SDK public-API half had no baseline to diff against, so it reported BLOCKED
(strict => fail) forever. This module establishes that baseline and makes the diff REAL and able to
fail (AGENTS.md: a gate that can't fail is worse than no gate; never a fake green).

It is deliberately BUILD-FREE and self-contained — no dotnet/npm/python-import toolchains in the
gate. Every surface is extracted deterministically from committed source at the manifest-pinned sha,
so re-extracting in CI reproduces the committed baseline byte-for-byte:

  honua-server      the committed OpenAPI documents (openapi.json + the OGC tiles/maps/processes/
                    coverages variants) — the server's published REST contract, canonicalised.
  honua-sdk-python  the committed compatibility/public-api.json snapshot (the SDK already maintains
                    it via scripts/compatibility_gate.py).
  honua-sdk-dotnet  a syntactic C# public-surface digest (public/protected declarations under src/,
                    namespace-qualified, tests excluded). Not a full apicompat run — a deterministic,
                    diffable surface descriptor that catches added/removed public API.
  honua-sdk-js      a syntactic TypeScript export-surface digest (top-level `export` declarations and
                    re-exports under src/, tests excluded).

Semantics (mirrors the proto half + the repo's bootstrap/strict enforcement model):

  * baseline dir for the platform is ABSENT  -> BLOCKED  (bootstrap tolerates; strict fails)
      "no platform contract baseline yet; run `contract_surface.py update` to establish one"
  * baseline present, current surface == baseline -> PASS   (rc.0: baseline established, diff empty)
  * baseline present, current surface != baseline -> FAIL   (a surface changed since the baseline;
      review it and, if intended, refresh the baseline in the same PR)

The FIRST platform release (2026.1-rc.0) is the baseline-setting release: `update` captures the
current surfaces, `check` then diffs empty => PASS honestly. Future releases diff against it.

CLI:
  python tools/contract_surface.py update  [--platform 2026.1] [--repos-root ..]   # establish/refresh
  python tools/contract_surface.py check   [--platform 2026.1] [--repos-root ..]   # gate: pass|fail|blocked
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    print(f"::error::PyYAML required: {exc}", file=sys.stderr)
    raise SystemExit(2)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Components whose REST/SDK public surfaces this gate tracks, and the manifest key each is pinned by.
SURFACE_COMPONENTS = ("honua-server", "honua-sdk-python", "honua-sdk-dotnet", "honua-sdk-js")

SERVER_OPENAPI_FILES = (
    "src/Honua.Server/openapi.json",
    "src/Honua.Server/ogc-tiles-openapi.json",
    "src/Honua.Server/ogc-maps-openapi.json",
    "src/Honua.Server/ogc-processes-openapi.json",
    "src/Honua.Server/ogc-coverages-openapi.json",
)
PYTHON_SNAPSHOT_FILES = ("compatibility/public-api.json",)


# ── git helpers ─────────────────────────────────────────────────────────────
def _git_show(repo: Path, sha: str, path: str) -> str | None:
    """Return the text of `path` at `sha`, or None if it does not exist there."""
    r = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
    )
    return r.stdout if r.returncode == 0 else None


def _git_archive_tree(repo: Path, sha: str, subdir: str, dest: Path) -> Path | None:
    """Extract `subdir` at `sha` into `dest`; return the extracted subdir path (or None)."""
    r = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", sha, subdir],
        capture_output=True,
    )
    if r.returncode != 0 or not r.stdout:
        return None
    with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as tf:
        tf.write(r.stdout)
        tar_path = tf.name
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest)  # noqa: S202 - trusted git archive of our own repo
    Path(tar_path).unlink(missing_ok=True)
    out = dest / subdir
    return out if out.exists() else None


def _canon(obj: object) -> str:
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ── surface extractors (deterministic, build-free) ──────────────────────────
def _extract_server(repo: Path, sha: str) -> dict[str, str]:
    """{artifact-filename: canonical-json-text} for each committed OpenAPI document present."""
    out: dict[str, str] = {}
    for path in SERVER_OPENAPI_FILES:
        text = _git_show(repo, sha, path)
        if text is None:
            continue
        name = Path(path).name
        try:
            out[name] = _canon(json.loads(text))
        except json.JSONDecodeError as exc:  # pragma: no cover
            raise ValueError(f"{path} at {sha[:12]} is not valid JSON: {exc}") from exc
    if not out:
        raise ValueError(f"no OpenAPI documents found at {sha[:12]} (expected {SERVER_OPENAPI_FILES[0]})")
    return out


def _extract_python(repo: Path, sha: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in PYTHON_SNAPSHOT_FILES:
        text = _git_show(repo, sha, path)
        if text is None:
            raise ValueError(f"expected committed public-API snapshot {path} at {sha[:12]}")
        out[Path(path).name] = _canon(json.loads(text))
    return out


_CS_DECL = re.compile(
    r"^\s*(?:\[[^\]]*\]\s*)*"
    r"(public|protected internal|protected)\s+"
    r"(?P<rest>.+?)\s*(?:\{|=>|;|$)"
)
_CS_NS_BLOCK = re.compile(r"^\s*namespace\s+([A-Za-z0-9_.]+)")
_CS_NS_FILESCOPED = re.compile(r"^\s*namespace\s+([A-Za-z0-9_.]+)\s*;")


def _cs_public_surface(root: Path) -> list[str]:
    """Namespace-qualified public/protected declarations under a C# src tree (tests excluded)."""
    symbols: set[str] = set()
    for cs in sorted(root.rglob("*.cs")):
        parts = {p.lower() for p in cs.parts}
        if {"bin", "obj"} & parts:
            continue
        rel = cs.as_posix().lower()
        if ".tests" in rel or "/tests/" in rel or rel.endswith(".designer.cs") or "generated" in rel:
            continue
        ns = ""
        try:
            lines = cs.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:  # pragma: no cover
            continue
        for line in lines:
            m_ns = _CS_NS_FILESCOPED.match(line) or _CS_NS_BLOCK.match(line)
            if m_ns:
                ns = m_ns.group(1)
                continue
            m = _CS_DECL.match(line)
            if not m:
                continue
            rest = re.sub(r"\s+", " ", m.group("rest")).strip()
            rest = rest.split("//")[0].strip()
            if not rest or rest in ("class", "get", "set"):
                continue
            symbols.add(f"{ns}: {m.group(1)} {rest}" if ns else f"{m.group(1)} {rest}")
    return sorted(symbols)


def _extract_dotnet(repo: Path, sha: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as td:
        src = _git_archive_tree(repo, sha, "src", Path(td))
        if src is None:
            raise ValueError(f"no src/ tree at {sha[:12]} for honua-sdk-dotnet")
        symbols = _cs_public_surface(src)
    payload = {
        "kind": "csharp-syntactic-public-surface",
        "note": "public/protected declarations under src/ (tests/generated excluded); "
                "deterministic diffable digest, not a full apicompat run",
        "count": len(symbols),
        "symbols": symbols,
    }
    return {"public-api.json": _canon(payload)}


_TS_DECL = re.compile(
    r"^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(class|function|const|let|var|interface|type|enum|namespace)\s+([A-Za-z0-9_$]+)"
)
_TS_STAR = re.compile(r"^\s*export\s+\*\s+(?:as\s+([A-Za-z0-9_$]+)\s+)?from\s+['\"]([^'\"]+)['\"]")
_TS_NAMED = re.compile(r"export\s*(type\s*)?\{([^}]*)\}", re.DOTALL)


def _ts_export_surface(root: Path) -> list[str]:
    exports: set[str] = set()
    for ts in sorted(root.rglob("*.ts")):
        rel = ts.as_posix().lower()
        if any(seg in rel for seg in (".test.", ".spec.", "/__tests__/", "/node_modules/")):
            continue
        if rel.endswith(".d.ts") and "/dist/" in rel:
            continue
        entry = ts.relative_to(root).as_posix()
        try:
            text = ts.read_text(encoding="utf-8", errors="replace")
        except OSError:  # pragma: no cover
            continue
        for line in text.splitlines():
            m = _TS_DECL.match(line)
            if m:
                exports.add(f"{entry}: {m.group(1)} {m.group(2)}")
            s = _TS_STAR.match(line)
            if s:
                exports.add(f"{entry}: * {('as ' + s.group(1) + ' ') if s.group(1) else ''}from {s.group(2)}")
        for m in _TS_NAMED.finditer(text):
            for tok in m.group(2).split(","):
                name = tok.strip()
                if not name:
                    continue
                # `A as B` re-exports as B
                name = re.split(r"\s+as\s+", name)[-1].strip()
                if re.fullmatch(r"[A-Za-z0-9_$]+", name):
                    exports.add(f"{entry}: named {name}")
    return sorted(exports)


def _extract_js(repo: Path, sha: str) -> dict[str, str]:
    with tempfile.TemporaryDirectory() as td:
        src = _git_archive_tree(repo, sha, "src", Path(td))
        if src is None:
            raise ValueError(f"no src/ tree at {sha[:12]} for honua-sdk-js")
        exports = _ts_export_surface(src)
    payload = {
        "kind": "typescript-export-surface",
        "note": "top-level `export` declarations + re-exports under src/ (tests excluded); "
                "deterministic diffable digest",
        "count": len(exports),
        "exports": exports,
    }
    return {"public-api.json": _canon(payload)}


EXTRACTORS = {
    "honua-server": _extract_server,
    "honua-sdk-python": _extract_python,
    "honua-sdk-dotnet": _extract_dotnet,
    "honua-sdk-js": _extract_js,
}


# ── manifest / plumbing ─────────────────────────────────────────────────────
def _load_pins(manifest_path: Path) -> dict[str, str]:
    data = yaml.safe_load(manifest_path.read_text()) or {}
    comps = data.get("components", {}) or {}
    pins: dict[str, str] = {}
    for name in SURFACE_COMPONENTS:
        sha = ((comps.get(name) or {}).get("sha") or "").strip()
        if sha:
            pins[name] = sha
    return pins


def extract_component(repo: Path, sha: str, component: str) -> dict[str, str]:
    return EXTRACTORS[component](repo, sha)


def _repo_path(repos_root: Path, component: str) -> Path:
    return repos_root / component


# ── update: establish/refresh the committed baseline ────────────────────────
def cmd_update(platform: str, repos_root: Path, manifest_path: Path, baseline_root: Path) -> int:
    pins = _load_pins(manifest_path)
    base = baseline_root / platform
    for component in SURFACE_COMPONENTS:
        sha = pins.get(component)
        if not sha:
            print(f"::warning::{component} has no sha in the manifest; skipping", file=sys.stderr)
            continue
        repo = _repo_path(repos_root, component)
        artifacts = extract_component(repo, sha, component)
        cdir = base / component
        cdir.mkdir(parents=True, exist_ok=True)
        for existing in cdir.glob("*"):
            if existing.name != "_meta.json":
                existing.unlink()
        for name, text in artifacts.items():
            (cdir / name).write_text(text, encoding="utf-8", newline="\n")
        (cdir / "_meta.json").write_text(
            _canon({"component": component, "sha": sha, "artifacts": sorted(artifacts)}),
            encoding="utf-8",
            newline="\n",
        )
        print(f"{component}: captured {len(artifacts)} artifact(s) @ {sha[:12]} -> {_rel(cdir)}")
    return 0


# ── check: the gate ─────────────────────────────────────────────────────────
def check(platform: str, repos_root: Path, manifest_path: Path, baseline_root: Path) -> tuple[str, str, list[dict]]:
    """Return (status, why, rows). status in {pass, fail, blocked}."""
    base = baseline_root / platform
    pins = _load_pins(manifest_path)
    if not base.is_dir():
        return ("blocked",
                f"no platform contract baseline at {_rel(base)}; "
                f"run `tools/contract_surface.py update` to establish one",
                [])
    rows: list[dict] = []
    for component in SURFACE_COMPONENTS:
        sha = pins.get(component)
        cdir = base / component
        if not sha:
            rows.append({"component": component, "status": "blocked", "why": "no sha in manifest"})
            continue
        if not cdir.is_dir():
            rows.append({"component": component, "status": "blocked",
                         "why": f"no baseline captured for {component}"})
            continue
        repo = _repo_path(repos_root, component)
        try:
            current = extract_component(repo, sha, component)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the gate
            rows.append({"component": component, "status": "blocked",
                         "why": f"could not extract surface @ {sha[:12]}: {exc}"})
            continue
        baseline = {p.name: p.read_text(encoding="utf-8")
                    for p in cdir.glob("*") if p.name != "_meta.json"}
        added = sorted(set(current) - set(baseline))
        removed = sorted(set(baseline) - set(current))
        changed = sorted(n for n in set(current) & set(baseline) if current[n] != baseline[n])
        if added or removed or changed:
            detail = []
            if added:
                detail.append(f"new artifacts {added}")
            if removed:
                detail.append(f"removed artifacts {removed}")
            if changed:
                detail.append(f"surface changed in {changed}")
            rows.append({"component": component, "status": "fail", "why": "; ".join(detail)})
        else:
            rows.append({"component": component, "status": "pass",
                         "why": f"{len(current)} artifact(s) match baseline @ {sha[:12]}"})
    statuses = {r["status"] for r in rows}
    if "fail" in statuses:
        status = "fail"
    elif "blocked" in statuses or not rows:
        status = "blocked"
    else:
        status = "pass"
    why = "; ".join(f"{r['component']}={r['status']}" for r in rows) or "no components checked"
    return (status, why, rows)


def cmd_check(platform: str, repos_root: Path, manifest_path: Path, baseline_root: Path) -> int:
    status, why, rows = check(platform, repos_root, manifest_path, baseline_root)
    for r in rows:
        print(f"  {r['component']:20s} {r['status']:8s} {r['why']}")
    print(f"contract-rest-sdk: {status} — {why}")
    # Emit for the workflow to consume via $GITHUB_ENV if requested.
    print(f"::notice::contract-rest-sdk={status}")
    return 0 if status == "pass" else (1 if status == "fail" else 3)


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["update", "check"])
    p.add_argument("--platform", default=None, help="platform label (default: from manifest platformRelease, major.minor)")
    p.add_argument("--repos-root", type=Path, default=REPO_ROOT.parent,
                   help="directory holding the component repos as subdirs (default: parent of honua-release)")
    p.add_argument("--manifest", type=Path, default=REPO_ROOT / "platform-manifest.yaml")
    p.add_argument("--baseline-root", type=Path, default=REPO_ROOT / "contracts" / "baselines")
    return p.parse_args(argv)


def _default_platform(manifest_path: Path) -> str:
    data = yaml.safe_load(manifest_path.read_text()) or {}
    label = str(data.get("platformRelease", "")).strip()
    m = re.match(r"(\d+)\.(\d+)", label)
    return f"{m.group(1)}.{m.group(2)}" if m else (label or "unknown")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    platform = args.platform or _default_platform(args.manifest)
    if args.mode == "update":
        return cmd_update(platform, args.repos_root, args.manifest, args.baseline_root)
    return cmd_check(platform, args.repos_root, args.manifest, args.baseline_root)


if __name__ == "__main__":
    raise SystemExit(main())
