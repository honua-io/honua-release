"""Admit one reviewed Python development-source install when immutable inputs match.

The default environment audit still refuses every PEP 610 direct origin. This
module is the opt-in consumer: admission is true only for a reviewed document
block, and only when the checkout revision, clean tree, package metadata,
backend, dependency receipt, origin, and installed contents all match. Matching
metadata never grants archive, wheel-byte, or candidate credit. Document
qualification stays a separate protected-source replay of every governed document.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from email.parser import Parser
from pathlib import Path
from urllib.parse import unquote, urlsplit


HERE = Path(__file__).resolve().parent
SELECTION = HERE / "python-source-inputs" / "selected.json"
SOURCES = HERE / "sources.json"
SCHEMA = "honua.python-development-source-admission/v1"
REPOSITORY = "honua-io/honua-sdk-python"
EXACT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
EXACT_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+]*")
HEX_SHA = re.compile(r"[0-9a-f]{40}")
HEX_DIGEST = re.compile(r"[a-f0-9]{64}")
REQUIRED_BLOCKS = (
    {
        "document": "README.md",
        "index": 1,
        "sha256": "52ae3befaa5abb60f863381805ee1230de6081a89f466722946b5edf1e859755",
    },
    {
        "document": "README.md",
        "index": 8,
        "sha256": "8e5ca8fcca606626e5908577b8d2293977b9f1e81f71b2a7b7a923ff689b2ea3",
    },
    {
        "document": "INSTALL.md",
        "index": 1,
        "sha256": "bb1abba46229bf51f71ff0c281708c4b2f37f3dce88b26eeb4b8473cbedeeb58",
    },
)


class AdmissionRefusal(ValueError):
    """Fail closed. The message never includes an origin URL or checkout path."""


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def reviewed_blocks() -> list[dict]:
    selected = json.loads(SELECTION.read_text(encoding="utf-8"))
    rows = selected.get("documentation")
    if not isinstance(rows, list):
        raise AdmissionRefusal("reviewed documentation blocks are missing")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            raise AdmissionRefusal("reviewed documentation blocks are not the admitted set")
        normalized.append({
            "document": row.get("document"),
            "index": row.get("index"),
            "sha256": row.get("sha256"),
        })
    expected = [dict(block) for block in REQUIRED_BLOCKS]
    if normalized != expected:
        raise AdmissionRefusal("reviewed documentation blocks are not the admitted set")
    if (selected.get("sourceAdmitted") is not False or selected.get("publishedWheelIdentity") is not False
            or selected.get("candidateQualified") is not False):
        raise AdmissionRefusal("selection flags are not closed")
    return expected


def governed_document_ids() -> list[str]:
    from inputs import doc_id

    document = json.loads(SOURCES.read_text(encoding="utf-8"))
    rows = document.get("documents")
    if not isinstance(rows, list) or not rows:
        raise AdmissionRefusal("governed document list is missing")
    return [doc_id(row["repo"], row["path"]) for row in rows]


def qualification_from_source_evidence(evidence: dict) -> bool:
    """Source-build evidence never qualifies a document."""
    return False


def replay_covers_governed_documents(receipt: dict) -> bool:
    """True only for a protected delivery plus an exact-candidate replay of every governed document."""
    if not isinstance(receipt, dict):
        return False
    if receipt.get("protectedSourceDelivery") is not True or receipt.get("exactCandidate") is not True:
        return False
    got = receipt.get("documentIds")
    if not isinstance(got, list) or any(not isinstance(item, str) for item in got):
        return False
    expected = governed_document_ids()
    return len(got) == len(expected) and sorted(got) == sorted(expected)


def bind(checkout: Path, *, document: str, block_index: int, package_relative: str, package_name: str,
         package_version: str, metadata_files: list[str], backend_files: list[str], package_files: list[dict],
         dependencies: list[dict]) -> dict:
    block = _require_block(document, block_index)
    root = _git_root(checkout)
    source_sha = _head(root)
    _require_clean(root)
    _require_identity(package_name, package_version)
    _contained_directory(root, package_relative)
    return {
        "schema": SCHEMA,
        "repository": REPOSITORY,
        "document": block["document"],
        "blockIndex": block["index"],
        "blockSha256": block["sha256"],
        "sourceSha": source_sha,
        "checkout": str(root),
        "packageRelative": package_relative,
        "packageName": package_name,
        "packageVersion": package_version,
        "metadataFiles": _file_records(root, metadata_files, "package metadata input"),
        "backendFiles": _file_records(root, backend_files, "build backend input"),
        "packageFiles": _package_records(root, package_relative, package_files),
        "dependencies": _dependency_records(root, dependencies),
    }


def recheck(binding: dict) -> None:
    _require_binding_shape(binding)
    root = _git_root(Path(binding["checkout"]))
    if str(root) != binding["checkout"]:
        raise AdmissionRefusal("checkout is not the git source root")
    if _head(root) != binding["sourceSha"]:
        raise AdmissionRefusal("source revision moved")
    _require_identity(binding["packageName"], binding["packageVersion"])
    _contained_directory(root, binding["packageRelative"])
    _match_records(root, binding["metadataFiles"], "package metadata input changed")
    _match_records(root, binding["backendFiles"], "build backend input changed")
    for item in binding["packageFiles"]:
        path = _contained_file(_contained_directory(root, binding["packageRelative"]), item["source"])
        if _sha256(path) != item["sha256"]:
            raise AdmissionRefusal("package content changed")
    for item in binding["dependencies"]:
        path = _contained_file(root, item["path"])
        if _sha256(path) != item["sha256"]:
            raise AdmissionRefusal("dependency input changed")
    _require_clean(root)


def bindings_for(source_admission):
    """One binding dict, or one dict per distribution in a multi-package block."""
    if isinstance(source_admission, dict):
        return [source_admission]
    if isinstance(source_admission, list) and source_admission and all(isinstance(item, dict) for item in source_admission):
        names = [canonical_name(str(item.get("packageName", ""))) for item in source_admission]
        if any(not name for name in names) or len(names) != len(set(names)):
            raise AdmissionRefusal("admission bindings do not name distinct packages")
        return source_admission
    raise AdmissionRefusal("admission binding is missing")


def binding_for_distribution(source_admission, package_name: str) -> dict:
    matches = [item for item in bindings_for(source_admission)
               if canonical_name(str(item.get("packageName", ""))) == canonical_name(package_name)]
    if len(matches) != 1:
        raise AdmissionRefusal("installed package identity does not match the binding")
    return matches[0]


def judge(binding: dict, site_path: Path) -> dict | None:
    """Return source-build evidence for the one admitted distribution, or None when this site has no direct origin."""
    recheck(binding)
    found = None
    if not site_path.is_dir():
        raise AdmissionRefusal("installed environment cannot be inspected")
    for entry in site_path.iterdir():
        if not _honua_distribution(entry):
            continue
        origin_path = entry / "direct_url.json"
        if not (origin_path.exists() or origin_path.is_symlink()):
            continue
        decision = judge_distribution(binding, site_path, entry)
        if found is not None:
            raise AdmissionRefusal("more than one direct-origin Honua distribution")
        found = decision
    return found


def judge_distribution(binding: dict, site_path: Path, entry: Path) -> dict:
    recheck(binding)
    _require_inside(site_path, entry)
    metadata_path = entry / "METADATA"
    _require_inside(site_path, metadata_path)
    document = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
    names, versions = document.get_all("Name", []), document.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise AdmissionRefusal("installed package identity does not match the binding")
    name, version = names[0].strip(), versions[0].strip()
    if canonical_name(name) != canonical_name(binding["packageName"]) or version != binding["packageVersion"]:
        raise AdmissionRefusal("installed package identity does not match the binding")
    origin_path = entry / "direct_url.json"
    _require_inside(site_path, origin_path)
    try:
        origin = json.loads(origin_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AdmissionRefusal("installed origin cannot be inspected") from None
    _require_bound_origin(binding, origin)
    _require_installed_contents(binding, site_path, origin)
    _require_installed_dependencies(binding, site_path)
    return _evidence(binding)


def _evidence(binding: dict) -> dict:
    return {
        "schema": SCHEMA,
        "repository": binding["repository"],
        "document": binding["document"],
        "blockIndex": binding["blockIndex"],
        "blockSha256": binding["blockSha256"],
        "sourceSha": binding["sourceSha"],
        "packageName": binding["packageName"],
        "packageVersion": binding["packageVersion"],
        "sourceAdmitted": True,
        "publishedWheelIdentity": False,
        "candidateQualified": False,
        "archiveCredit": False,
        "wheelByteCredit": False,
        "dependencies": [
            {"name": item["name"], "version": item["version"], "sha256": item["sha256"]}
            for item in binding["dependencies"]
        ],
    }


def _require_block(document: str, block_index: int) -> dict:
    for block in reviewed_blocks():
        if block["document"] == document and block["index"] == block_index:
            return block
    raise AdmissionRefusal("document block is not a reviewed development-source block")


def _require_binding_shape(binding: dict) -> None:
    if not isinstance(binding, dict):
        raise AdmissionRefusal("admission binding is missing")
    required = ("schema", "repository", "document", "blockIndex", "blockSha256", "sourceSha", "checkout",
                "packageRelative", "packageName", "packageVersion", "metadataFiles", "backendFiles",
                "packageFiles", "dependencies")
    if any(key not in binding for key in required):
        raise AdmissionRefusal("admission binding is missing an immutable input")
    if binding["schema"] != SCHEMA or binding["repository"] != REPOSITORY:
        raise AdmissionRefusal("admission binding is not the development-source contract")
    block = _require_block(binding["document"], binding["blockIndex"])
    if binding["blockSha256"] != block["sha256"] or not HEX_SHA.fullmatch(binding["sourceSha"]):
        raise AdmissionRefusal("admission binding is not a reviewed immutable revision")
    if not isinstance(binding["dependencies"], list):
        raise AdmissionRefusal("dependency receipt is missing")


def _require_identity(package_name: str, package_version: str) -> None:
    if not isinstance(package_name, str) or EXACT_NAME.fullmatch(package_name) is None or not package_name.lower().startswith("honua"):
        raise AdmissionRefusal("package identity is not a Honua distribution")
    if not isinstance(package_version, str) or EXACT_VERSION.fullmatch(package_version) is None:
        raise AdmissionRefusal("package version is not exact")


def _git(checkout: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(checkout), *args], capture_output=True, text=True)


def _git_root(checkout: Path) -> Path:
    if not checkout.exists():
        raise AdmissionRefusal("checkout is not an immutable git source")
    result = _git(checkout, "rev-parse", "--show-toplevel")
    if result.returncode != 0:
        raise AdmissionRefusal("checkout is not an immutable git source")
    root = Path(result.stdout.strip()).resolve()
    if root != checkout.resolve():
        raise AdmissionRefusal("checkout is not the git source root")
    return root


def _head(root: Path) -> str:
    result = _git(root, "rev-parse", "HEAD")
    head = result.stdout.strip()
    if result.returncode != 0 or HEX_SHA.fullmatch(head) is None:
        raise AdmissionRefusal("source revision is not an immutable commit")
    return head


def _require_clean(root: Path) -> None:
    result = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if result.returncode != 0 or result.stdout.strip():
        raise AdmissionRefusal("source checkout is not clean")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contained(root: Path, relative: str, *, directory: bool) -> Path:
    if not isinstance(relative, str) or not relative or relative.startswith(("/", "\\")) or "\\" in relative:
        raise AdmissionRefusal("input path escapes the bound checkout")
    parts = Path(relative).parts
    if not parts or any(part in ("..", "") for part in parts):
        raise AdmissionRefusal("input path escapes the bound checkout")
    candidate = root.joinpath(*parts)
    if candidate.is_symlink() or any((root.joinpath(*parts[:index])).is_symlink() for index in range(1, len(parts))):
        if not candidate.exists() or not candidate.resolve().is_relative_to(root.resolve()):
            raise AdmissionRefusal("input path escapes the bound checkout")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise AdmissionRefusal("input path escapes the bound checkout")
    if directory:
        if not resolved.is_dir():
            raise AdmissionRefusal("package input is not a directory")
    elif not resolved.is_file():
        raise AdmissionRefusal("immutable file input is missing")
    return resolved


def _contained_file(root: Path, relative: str) -> Path:
    return _contained(root, relative, directory=False)


def _contained_directory(root: Path, relative: str) -> Path:
    return _contained(root, relative, directory=True)


def _require_inside(root: Path, candidate: Path) -> None:
    if candidate.is_symlink() and not candidate.resolve().is_relative_to(root.resolve()):
        raise AdmissionRefusal("installed metadata escapes its environment")
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise AdmissionRefusal("installed metadata escapes its environment")


def _file_records(root: Path, relatives: list[str], label: str) -> list[dict]:
    if not isinstance(relatives, list) or not relatives or any(not isinstance(item, str) for item in relatives):
        raise AdmissionRefusal(f"{label} is missing")
    return [{"path": relative, "sha256": _sha256(_contained_file(root, relative))} for relative in relatives]


def _match_records(root: Path, records: list[dict], message: str) -> None:
    if not isinstance(records, list) or not records:
        raise AdmissionRefusal(message)
    for item in records:
        if _sha256(_contained_file(root, item["path"])) != item["sha256"]:
            raise AdmissionRefusal(message)


def _package_records(root: Path, package_relative: str, package_files: list[dict]) -> list[dict]:
    if not isinstance(package_files, list) or not package_files:
        raise AdmissionRefusal("package content inputs are missing")
    package_root = _contained_directory(root, package_relative)
    records = []
    for item in package_files:
        if not isinstance(item, dict):
            raise AdmissionRefusal("package content inputs are missing")
        source, installed = item.get("source"), item.get("installed")
        if not isinstance(installed, str) or Path(installed).is_absolute() or ".." in Path(installed).parts:
            raise AdmissionRefusal("installed path escapes the environment")
        records.append({
            "source": source,
            "installed": installed,
            "sha256": _sha256(_contained_file(package_root, source)),
        })
    return records


def _dependency_records(root: Path, dependencies: list[dict]) -> list[dict]:
    if not isinstance(dependencies, list):
        raise AdmissionRefusal("dependency receipt is missing")
    records = []
    for item in dependencies:
        if not isinstance(item, dict):
            raise AdmissionRefusal("dependency receipt is not exact")
        name, version, digest, relative = item.get("name"), item.get("version"), item.get("sha256"), item.get("path")
        if not isinstance(name, str) or EXACT_NAME.fullmatch(name) is None or canonical_name(name).startswith("honua"):
            raise AdmissionRefusal("dependency name is not exact")
        if not isinstance(version, str) or EXACT_VERSION.fullmatch(version) is None:
            raise AdmissionRefusal("dependency version is not an exact pin")
        if not isinstance(digest, str) or HEX_DIGEST.fullmatch(digest) is None:
            raise AdmissionRefusal("dependency receipt has no immutable digest")
        if _sha256(_contained_file(root, relative)) != digest:
            raise AdmissionRefusal("dependency receipt does not match the checkout artifact")
        records.append({"name": name, "version": version, "sha256": digest, "path": relative})
    return records


def _honua_distribution(entry: Path) -> bool:
    suffix = next((value for value in (".dist-info", ".egg-info") if entry.name.lower().endswith(value)), None)
    if suffix is None or not entry.is_dir():
        return False
    metadata_path = entry / "METADATA"
    if not metadata_path.is_file():
        return canonical_name(entry.name).startswith("honua")
    document = Parser().parsestr(metadata_path.read_text(encoding="utf-8"))
    names = document.get_all("Name", [])
    return canonical_name(entry.name).startswith("honua") or any(
        canonical_name(value.strip()).startswith("honua") for value in names)


def _file_origin(origin: dict) -> Path:
    if not isinstance(origin, dict) or "archive_info" in origin:
        raise AdmissionRefusal("archive origins are not development-source admission")
    url = origin.get("url")
    if not isinstance(url, str):
        raise AdmissionRefusal("installed origin cannot be inspected")
    parsed = urlsplit(url)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise AdmissionRefusal("installed origin is not a local checkout path")
    raw = unquote(parsed.path)
    if not raw.startswith("/"):
        raise AdmissionRefusal("installed origin is not a local checkout path")
    return Path(raw)


def _require_bound_origin(binding: dict, origin: dict) -> None:
    if not isinstance(origin, dict):
        raise AdmissionRefusal("installed origin cannot be inspected")
    if "archive_info" in origin:
        raise AdmissionRefusal("archive origins are not development-source admission")
    kinds = ("dir_info" in origin) + ("vcs_info" in origin)
    if kinds != 1:
        raise AdmissionRefusal("installed origin is not a bound checkout")
    resolved = _file_origin(origin).resolve()
    checkout = Path(binding["checkout"]).resolve()
    package = (checkout / binding["packageRelative"]).resolve()
    if not resolved.is_relative_to(checkout):
        raise AdmissionRefusal("installed origin is outside the bound checkout")
    if "dir_info" in origin:
        if resolved != package:
            raise AdmissionRefusal("installed origin is not the bound package")
        return
    vcs = origin.get("vcs_info")
    if not isinstance(vcs, dict) or vcs.get("commit_id") != binding["sourceSha"]:
        raise AdmissionRefusal("version control origin is not the bound revision")
    if resolved not in (checkout, package):
        raise AdmissionRefusal("version control origin is not the bound checkout")


def _require_installed_contents(binding: dict, site_path: Path, origin: dict) -> None:
    editable = isinstance(origin.get("dir_info"), dict) and origin["dir_info"].get("editable") is True
    if editable:
        _require_editable_mapping(binding, site_path)
        return
    expected = {item["installed"] for item in binding["packageFiles"]}
    if not expected:
        raise AdmissionRefusal("observed installed contents do not match the bound package")
    observed: set[str] = set()
    for root_name in {Path(item).parts[0] for item in expected}:
        root = site_path / root_name
        if not root.is_dir() or not root.resolve().is_relative_to(site_path.resolve()):
            raise AdmissionRefusal("observed installed contents do not match the bound package")
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.is_symlink() and not path.resolve().is_relative_to(site_path.resolve()):
                raise AdmissionRefusal("observed installed contents escape the environment")
            observed.add(path.relative_to(site_path).as_posix())
    if observed != expected:
        raise AdmissionRefusal("observed installed contents do not match the bound package")
    for item in binding["packageFiles"]:
        if _sha256(site_path / item["installed"]) != item["sha256"]:
            raise AdmissionRefusal("observed installed contents do not match the bound package")


def _require_editable_mapping(binding: dict, site_path: Path) -> None:
    package = (Path(binding["checkout"]) / binding["packageRelative"]).resolve()
    mapped = _editable_paths(site_path, binding["packageName"])
    if not mapped:
        raise AdmissionRefusal("editable install does not map the bound package")
    expected = {(package / item["source"]).resolve() for item in binding["packageFiles"]}
    saw_package = False
    for raw in mapped:
        resolved = Path(raw).resolve()
        if not resolved.is_relative_to(package):
            raise AdmissionRefusal("editable install escapes the bound package")
        saw_package = saw_package or resolved == package or resolved in expected
    if not saw_package:
        raise AdmissionRefusal("editable install does not map the bound package")


def _editable_paths(site_path: Path, package_name: str) -> list[str]:
    token = canonical_name(package_name).replace("-", "_")
    paths = []
    for path in site_path.iterdir():
        name = path.name.lower()
        if not path.is_file():
            continue
        if not (name.endswith(".pth") or "editable" in name or (name.endswith(".py") and "finder" in name)):
            continue
        if token not in name.replace("-", "_") and canonical_name(package_name) not in name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        paths.extend(re.findall(r"(?m)(?:^|['\"])(/(?:[^'\"\r\n])+)", text))
    return paths


def _require_installed_dependencies(binding: dict, site_path: Path) -> None:
    import importlib.metadata as metadata

    found: dict[str, list[str]] = {}
    for distribution in metadata.distributions(path=[str(site_path)]):
        name = distribution.metadata.get("Name") if distribution.metadata else None
        if not name or not distribution.version:
            continue
        found.setdefault(canonical_name(name), []).append(distribution.version)
    for item in binding["dependencies"]:
        versions = found.get(canonical_name(item["name"]), [])
        if versions != [item["version"]]:
            raise AdmissionRefusal("dependency is not installed at the bound version")
        _require_dependency_bytes(binding, site_path, item)


def _require_dependency_bytes(binding: dict, site_path: Path, item: dict) -> None:
    wheel_path = Path(binding["checkout"]) / item["path"]
    try:
        archive = zipfile.ZipFile(wheel_path)
    except (OSError, zipfile.BadZipFile):
        raise AdmissionRefusal("dependency contents do not match the bound artifact") from None
    with archive:
        members = [name for name in archive.namelist() if ".dist-info/" not in name and not name.endswith("/")]
        if not members:
            raise AdmissionRefusal("dependency contents do not match the bound artifact")
        payloads = {name: archive.read(name) for name in members}
    tops = {Path(name).parts[0] for name in payloads}
    for name, payload in payloads.items():
        installed = site_path / name
        if (not installed.is_file() or not installed.resolve().is_relative_to(site_path.resolve())
                or installed.read_bytes() != payload):
            raise AdmissionRefusal("dependency contents do not match the bound artifact")
    for top in tops:
        root = site_path / top
        if not root.is_dir() or not root.resolve().is_relative_to(site_path.resolve()):
            raise AdmissionRefusal("dependency contents do not match the bound artifact")
        for path in root.rglob("*"):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.relative_to(site_path).as_posix() not in payloads:
                raise AdmissionRefusal("dependency contents do not match the bound artifact")
