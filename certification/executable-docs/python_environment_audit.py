"""Inventory default and document-created Python environments without running their interpreters."""
from __future__ import annotations

import argparse
from email.parser import Parser
import importlib.metadata as metadata
import json
import os
import re
from pathlib import Path
import sys
import shutil
import site


MAX_DIRECTORIES = 50_000


def walk_error(error: OSError) -> None:
    raise error


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def default_metadata_sites() -> list[Path]:
    return sorted({Path(value) for value in sys.path if value.endswith(("site-packages", "dist-packages"))}
                  | {Path(site.getusersitepackages())})


def inspect_honua_metadata(site_path: Path) -> None:
    for entry in site_path.iterdir():
        suffix = next((value for value in (".dist-info", ".egg-info") if entry.name.lower().endswith(value)), None)
        if suffix is None:
            continue
        metadata_path = (entry / ("METADATA" if suffix == ".dist-info" else "PKG-INFO")) if entry.is_dir() else entry
        filename_honua = canonical_name(entry.name).startswith("honua")
        if (not entry.resolve().is_relative_to(site_path.resolve())
                or not metadata_path.resolve().is_relative_to(site_path.resolve())):
            raise ValueError(f"Honua distribution metadata escapes its environment: {entry}")
        if not metadata_path.exists() and not filename_honua:
            continue
        text = metadata_path.read_text()
        document = Parser().parsestr(text)
        names, versions = document.get_all("Name", []), document.get_all("Version", [])
        if not filename_honua and not any(canonical_name(value.strip()).startswith("honua") for value in names):
            continue
        if (len(names) != 1 or len(versions) != 1 or not names[0].strip() or not versions[0].strip()
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", names[0].strip())
                or any(character.isspace() for character in versions[0].strip())):
            raise ValueError(f"Honua metadata is incomplete or contradictory: {entry}")
        origin_path = entry / "direct_url.json" if entry.is_dir() else None
        if origin_path is not None and (origin_path.exists() or origin_path.is_symlink()):
            if not origin_path.resolve().is_relative_to(site_path.resolve()):
                raise ValueError(f"Honua origin metadata escapes its environment: {entry}")
            try:
                origin = json.loads(origin_path.read_text())
            except (OSError, ValueError) as error:
                raise ValueError(f"Honua origin metadata cannot be inspected: {entry}") from None
            if not isinstance(origin, dict):
                raise ValueError(f"Honua origin metadata is malformed: {entry}")
            # Never include origin URLs: they can contain registry credentials.
            if "dir_info" in origin or "vcs_info" in origin:
                raise ValueError(f"Honua local/VCS installation is not an admitted published artifact: {entry}")
        name, version = canonical_name(names[0].strip()), versions[0].strip()
        stem = entry.name[:-len(suffix)]
        matches = False
        for offset in range(1, len(stem) + 1):
            if canonical_name(stem[:offset]) != name:
                continue
            remainder = stem[offset:]
            matches |= remainder == f"-{version}" or (suffix == ".egg-info" and
                       (not remainder or re.fullmatch(re.escape(f"-{version}") + r"-py[0-9.]+", remainder) is not None))
        if not name.startswith("honua") or not matches:
            raise ValueError(f"Honua metadata name/version contradicts its installed path: {entry}")


def inventory(roots: list[Path], interpreters: list[str] | None = None) -> dict:
    environments: set[Path] = set()
    visited: set[Path] = set()
    for root in roots:
        if not root.exists():
            raise ValueError(f"Python audit root is unavailable: {root}")
        for directory, _, files in os.walk(root, followlinks=False, onerror=walk_error):
            path = Path(directory).resolve()
            if path in visited:
                continue
            visited.add(path)
            if len(visited) > MAX_DIRECTORIES:
                raise ValueError("Python environment audit exceeded its directory bound")
            if "pyvenv.cfg" in files:
                environments.add(path)
    active = os.environ.get("VIRTUAL_ENV")
    if active:
        environments.add(Path(active).resolve())
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if entry and (Path(entry).parent / "pyvenv.cfg").is_file():
            environments.add(Path(entry).parent.resolve())

    bounded = [root.resolve() for root in roots]
    for environment in environments:
        if not any(environment.is_relative_to(root) for root in bounded):
            raise ValueError(f"Python environment escapes the bounded audit roots: {environment}")
    invoked = []
    for value in interpreters or []:
        selected = Path(shutil.which(value) or value)
        if not selected.is_absolute():
            raise ValueError(f"Documented Python interpreter is not resolved: {value}")
        if not selected.exists():
            raise ValueError(f"Documented Python interpreter is unavailable: {selected}")
        prefix = selected.parent.parent
        if (prefix / "pyvenv.cfg").is_file():
            if not any(prefix.resolve().is_relative_to(root) for root in bounded):
                raise ValueError(f"Documented Python environment escapes the bounded audit roots: {selected}")
            environments.add(prefix.resolve())
        elif selected.resolve() != Path(sys.executable).resolve() and prefix != Path(sys.prefix):
            raise ValueError(f"Documented Python interpreter environment cannot be inspected: {selected}")
        elif any(prefix.is_relative_to(root.resolve()) for root in roots):
            raise ValueError(f"Documented Python environment is missing pyvenv.cfg: {selected}")
        invoked.append(str(selected))

    packages: dict[tuple[str, str, str], dict] = {}

    def retain(distributions) -> None:
        for distribution in distributions:
            name = distribution.metadata.get("Name")
            location = str(distribution.locate_file(""))
            if name and name.lower().replace("_", "-").startswith("honua"):
                version = distribution.version
                if not version:
                    raise ValueError(f"Honua distribution has no version: {location}")
                row = {"name": name, "version": version, "path": location}
                packages[(name, version, location)] = row

    retain(metadata.distributions())
    # The trusted probe runs isolated: inspect the reader's user-site metadata
    # explicitly without executing its .pth files or sitecustomize code.
    for default_site in default_metadata_sites():
        if default_site.exists():
            inspect_honua_metadata(default_site)
            retain(metadata.distributions(path=[str(default_site)]))
    audited = []
    for environment in sorted(environments):
        (environment / "pyvenv.cfg").read_text()  # unreadable/broken environments cannot silently disappear
        interpreter = environment / "bin/python"
        if not interpreter.exists() and not (environment / "Scripts/python.exe").exists():
            raise ValueError(f"Python environment interpreter is unavailable: {environment}")
        sites = sorted(set(environment.glob("lib/python*/site-packages"))
                       | set(environment.glob("Lib/site-packages")))
        if not sites:
            raise ValueError(f"Python environment site-packages is unavailable: {environment}")
        for site_path in sites:
            if not site_path.resolve().is_relative_to(environment):
                raise ValueError(f"Python environment site-packages escapes its root: {site_path}")
            inspect_honua_metadata(site_path)
            retain(metadata.distributions(path=[str(site_path)]))
        audited.append({"environment": str(environment), "sitePackages": [str(site_path) for site_path in sites]})
    return {"environments": audited, "documentedInterpreters": invoked, "packages": [packages[key] for key in sorted(packages)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--interpreter", action="append", default=[])
    arguments = parser.parse_args()
    print(json.dumps(inventory(arguments.roots, arguments.interpreter)))
