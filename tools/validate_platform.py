#!/usr/bin/env python3
"""Phase 0 source-of-truth gate — validate platform-manifest.yaml + compatibility-matrix.yaml and
enforce compatibility drift.

This is the keystone gate for issue #1: the manifest + matrix are only a credible "source of truth
for iac<->server<->sdk<->db compatibility" if something *checks* them, and that check can FAIL.
Per AGENTS.md: a gate that can't fail is worse than no gate. The tests in tools/test_platform.py
prove each rule below reddens on a real violation.

Three kinds of check:

  STRUCTURE   — both files parse; required keys present; versions/ranges are well-formed; every
                client/component the matrix references exists in the manifest.

  COHERENCE   — the pinned set IS internally consistent: each component's pinned manifest version
                SATISFIES every compatibility-matrix range that names it; sha-pinned couplings
                (iac/helm -> server image, server -> db schema) agree between the two files.
                Bump a pin out of its range, or tighten a range past the pin, and this goes red.

  DRIFT       — compared against a git baseline (the PR base by default), a matrix range may only
                *widen* unless the contract's version is bumped. Narrowing a client's support
                window (raising a floor / lowering a ceiling) without a contract-version bump is a
                breaking change without the required version move -> FAIL. This is the
                manifest/matrix-level half of "a breaking change without a MAJOR bump fails CI";
                the wire-level detectors (buf/OpenAPI/public-API diff) live in the component repos
                (issues #2/#3).

Usage:
  python tools/validate_platform.py                  # structure + coherence (no baseline)
  python tools/validate_platform.py --baseline origin/main   # + drift vs that git ref
  python tools/validate_platform.py --no-drift       # explicitly skip drift even if a baseline exists
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml  # PyYAML
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("PyYAML is required: pip install pyyaml") from exc

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semver  # noqa: E402  (local module, sibling file)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "platform-manifest.yaml"
MATRIX_PATH = REPO_ROOT / "compatibility-matrix.yaml"

# A component pinned by sha (no release/tag yet) carries this sentinel instead of a semver version.
PRERELEASE_SENTINEL = "pre-release"
SHA_PREFIX = "sha:"


@dataclass
class Findings:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors


# --------------------------------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _load_yaml_at_ref(ref: str, rel_path: str) -> dict | None:
    """Load a YAML file as it existed at a git ref. Returns None if the ref/path is unavailable
    (e.g. the file is brand new on this branch) — drift is then skipped for it, not failed."""
    try:
        out = subprocess.run(
            ["git", "show", f"{ref}:{rel_path}"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=True,
        ).stdout
    except subprocess.CalledProcessError:
        return None
    try:
        return yaml.safe_load(out) or {}
    except yaml.YAMLError:
        return None


# --------------------------------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------------------------------
def _component_version_kind(comp: dict) -> str:
    """'semver' | 'sha' | 'invalid' — how a manifest component declares its version."""
    version = str(comp.get("version", "")).strip()
    if version == PRERELEASE_SENTINEL:
        return "sha" if str(comp.get("sha", "")).strip() else "invalid"
    return "semver" if semver.is_semver(version) else "invalid"


def check_structure(manifest: dict, matrix: dict, f: Findings) -> None:
    for key in ("platformRelease", "status", "components"):
        if key not in manifest:
            f.error(f"manifest: missing required top-level key {key!r}")
    components = manifest.get("components") or {}
    if not isinstance(components, dict) or not components:
        f.error("manifest: 'components' must be a non-empty mapping")
        return

    for name, comp in components.items():
        comp = comp or {}
        kind = _component_version_kind(comp)
        if kind == "invalid":
            f.error(
                f"manifest: component {name!r} has neither a valid semver 'version' nor a "
                f"'{PRERELEASE_SENTINEL}' + sha pin (version={comp.get('version')!r}, sha={comp.get('sha')!r})"
            )
        if kind == "sha" and not comp.get("sha"):
            f.error(f"manifest: component {name!r} is {PRERELEASE_SENTINEL} but has no sha")

    # Matrix ranges must parse, and every named client/component must exist in the manifest.
    for contract, body in (matrix.get("contracts") or {}).items():
        for client, spec in (body.get("clients") or {}).items():
            if client not in components:
                f.error(f"matrix: contract {contract!r} names unknown client {client!r} (not in manifest)")
            try:
                semver.parse_range(spec)
            except semver.InvalidRange as e:
                f.error(f"matrix: contract {contract!r} client {client!r} has bad range {spec!r}: {e}")

    for section in ("deploy", "data"):
        for comp_name in (matrix.get(section) or {}):
            if comp_name not in components:
                f.error(f"matrix: {section} names unknown component {comp_name!r} (not in manifest)")


# --------------------------------------------------------------------------------------------------
# coherence — the pinned set satisfies the matrix
# --------------------------------------------------------------------------------------------------
def check_coherence(manifest: dict, matrix: dict, f: Findings) -> None:
    components = manifest.get("components") or {}

    # 1. Every pinned semver version satisfies every matrix range that names it.
    for contract, body in (matrix.get("contracts") or {}).items():
        for client, spec in (body.get("clients") or {}).items():
            comp = components.get(client)
            if comp is None:
                continue  # already an error in check_structure
            if _component_version_kind(comp) != "semver":
                # sha-pinned (un-released) client: the matrix floor is its declared (un-shipped)
                # version; there is no shippable semver to satisfy yet. Skip — not a violation.
                continue
            version = str(comp["version"]).strip()
            try:
                if not semver.satisfies(version, spec):
                    f.error(
                        f"coherence: {client} is pinned at {version} in the manifest but does NOT "
                        f"satisfy the {contract!r} contract range {spec!r} in the matrix"
                    )
            except (semver.InvalidRange, semver.InvalidVersion) as e:
                f.error(f"coherence: cannot test {client} {version!r} against {spec!r}: {e}")

    # 2. sha-pinned deploy/data couplings agree across the two files.
    server = components.get("honua-server") or {}
    server_sha = str(server.get("sha", "")).strip()
    for comp_name, body in (matrix.get("deploy") or {}).items():
        for field_name in ("deploysServerImage", "appVersion"):
            val = str((body or {}).get(field_name, "")).strip()
            if val.startswith(SHA_PREFIX):
                pinned = val[len(SHA_PREFIX):]
                if server_sha and pinned != server_sha:
                    f.error(
                        f"coherence: matrix deploy.{comp_name}.{field_name} pins server "
                        f"sha {pinned} but the manifest pins honua-server at {server_sha}"
                    )

    matrix_db = str(((matrix.get("data") or {}).get("honua-server") or {}).get("requiresDbSchema", "")).strip()
    manifest_db = str(server.get("dbSchema", "")).strip()
    if matrix_db and manifest_db and not matrix_db.startswith(">") and matrix_db != manifest_db:
        f.error(
            f"coherence: matrix data.honua-server.requiresDbSchema={matrix_db!r} disagrees with "
            f"manifest honua-server.dbSchema={manifest_db!r}"
        )


# --------------------------------------------------------------------------------------------------
# drift — a range may only widen unless the contract version bumped
# --------------------------------------------------------------------------------------------------
def _range_or_none(spec: str) -> semver.Range | None:
    try:
        return semver.parse_range(spec)
    except semver.InvalidRange:
        return None


def check_drift(matrix: dict, baseline: dict, f: Findings) -> None:
    base_contracts = baseline.get("contracts") or {}
    cur_contracts = matrix.get("contracts") or {}

    for contract, cur_body in cur_contracts.items():
        base_body = base_contracts.get(contract)
        if base_body is None:
            continue  # new contract surface — nothing to compare
        contract_bumped = str(cur_body.get("version", "")) != str(base_body.get("version", ""))

        cur_clients = cur_body.get("clients") or {}
        base_clients = base_body.get("clients") or {}

        for client, base_spec in base_clients.items():
            if client not in cur_clients:
                if not contract_bumped:
                    f.error(
                        f"drift: client {client!r} was dropped from contract {contract!r} without a "
                        f"contract-version bump (dropping a supported client is breaking)"
                    )
                continue
            base_range = _range_or_none(base_spec)
            cur_range = _range_or_none(cur_clients[client])
            if not base_range or not cur_range:
                continue
            narrowed = _narrowed(base_range, cur_range)
            if narrowed and not contract_bumped:
                f.error(
                    f"drift: contract {contract!r} client {client!r} narrowed its support window "
                    f"({base_range.raw!r} -> {cur_range.raw!r}: {narrowed}) without a contract-version "
                    f"bump — breaking change without the required version move"
                )


def _narrowed(base: semver.Range, cur: semver.Range) -> str:
    """Return a human reason if `cur` drops support that `base` granted, else ''."""
    reasons = []
    # Floor raised: versions at/above the old floor but below the new floor lost support.
    if base.floor is not None and cur.floor is not None and cur.floor > base.floor:
        reasons.append(f"floor raised {base.floor} -> {cur.floor}")
    if base.floor is None and cur.floor is not None:
        reasons.append(f"added a floor {cur.floor} (was open-below)")
    # Ceiling lowered: versions below the old ceiling but at/above the new one lost support.
    if base.ceiling is not None and cur.ceiling is not None and cur.ceiling < base.ceiling:
        reasons.append(f"ceiling lowered {base.ceiling} -> {cur.ceiling}")
    if base.ceiling is None and cur.ceiling is not None:
        reasons.append(f"added a ceiling {cur.ceiling} (was open-above)")
    return "; ".join(reasons)


# --------------------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------------------
def validate(manifest: dict, matrix: dict, baseline_matrix: dict | None) -> Findings:
    f = Findings()
    check_structure(manifest, matrix, f)
    # Coherence/drift assume structure held well enough to read; they no-op on missing pieces.
    check_coherence(manifest, matrix, f)
    if baseline_matrix is not None:
        check_drift(matrix, baseline_matrix, f)
    return f


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--baseline", default=None,
                    help="git ref to diff against for drift enforcement (e.g. origin/main)")
    ap.add_argument("--no-drift", action="store_true", help="skip drift even if --baseline is given")
    ap.add_argument("--manifest", default=str(MANIFEST_PATH))
    ap.add_argument("--matrix", default=str(MATRIX_PATH))
    args = ap.parse_args(argv)

    manifest = _load_yaml(Path(args.manifest))
    matrix = _load_yaml(Path(args.matrix))

    baseline_matrix: dict | None = None
    if args.baseline and not args.no_drift:
        baseline_matrix = _load_yaml_at_ref(args.baseline, "compatibility-matrix.yaml")
        if baseline_matrix is None:
            print(f"note: no baseline compatibility-matrix.yaml at {args.baseline!r}; skipping drift")

    f = validate(manifest, matrix, baseline_matrix)

    for w in f.warnings:
        print(f"WARN  {w}")
    for e in f.errors:
        print(f"ERROR {e}")

    if f.ok:
        scope = "structure + coherence" + (" + drift" if baseline_matrix is not None else "")
        print(f"OK    platform manifest + compatibility matrix valid ({scope})")
        return 0
    print(f"\nFAILED: {len(f.errors)} error(s), {len(f.warnings)} warning(s)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
