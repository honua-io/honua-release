"""Pin and export public OKF documents without exporting a repository or test answers.

Git objects, rather than working-tree files, supply the bytes. This module does not
execute code from the documentation repository. The controller authenticates the
source revision; a content hash alone is not an attestation.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tarfile
import tempfile
import time

from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, portable_path

REPOSITORY = "https://github.com/honua-io/honua-server"
GRAPH = "docs/okf/domain-graph.v1.json"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# Reviewed generator bytes from honua-server. The fallback never executes a
# generator supplied by an arbitrary future source revision.
GENERATOR_DIGESTS = {
    "scripts/ci/generate-okf-graph.py": "sha256:d7333ec5e476fa600f94f754d72c0715717e768a5e3ef54650848fde1e645111",
    "scripts/ci/okf_frontmatter.py": "sha256:d179edd803b584eb1524c355e5bcdd6da61d84013355ae3ce3ad0e269a4d4018",
    "scripts/ci/gis_workflow.py": "sha256:17f84c7ea6572bd8d72599cf9332c2dc0c018375126695014389a65c65bec886",
}
SOURCE_MANIFEST = "scripts/ci/okf-bundle.v1.json"
MAX_SOURCE_ARCHIVE_BYTES = 64 * 1024 * 1024


def require(value, message):
    if not value:
        raise PackError(message)


def git_bytes(repository, revision, path):
    require(SHA.fullmatch(revision) is not None, "knowledge source requires an exact commit")
    portable_path(path)
    result = subprocess.run(["git", "-C", str(repository), "show", f"{revision}:{path}"],
                            capture_output=True, check=True)
    return result.stdout.decode("utf-8").replace("\r\n", "\n").encode("utf-8")


def bounded_archive(repository, revision, paths):
    """Read one local Git archive with a byte and wall-clock bound.

    The hosted pilot worker-image workflow runs this exporter on ubuntu-latest;
    select() on a subprocess pipe requires a POSIX runner.
    """
    require(os.name == "posix", "pinned OKF graph generation requires a POSIX runner")
    command = ["git", "-C", str(repository), "archive", "--format=tar", revision, *paths]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    chunks = []
    size = 0
    deadline = time.monotonic() + 30
    try:
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0, "knowledge source archive timed out")
            ready, _, _ = select.select([process.stdout], [], [], remaining)
            require(bool(ready), "knowledge source archive timed out")
            chunk = os.read(process.stdout.fileno(), min(65536, MAX_SOURCE_ARCHIVE_BYTES - size + 1))
            if not chunk:
                break
            size += len(chunk)
            require(size <= MAX_SOURCE_ARCHIVE_BYTES, "knowledge source archive exceeds bound")
            chunks.append(chunk)
        require(process.wait(timeout=max(0.1, deadline - time.monotonic())) == 0 and size > 0,
                "knowledge source archive failed")
        return b"".join(chunks)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def graph_bytes(repository, revision):
    """Read a legacy graph, or derive it from a pinned source using reviewed code."""
    require(SHA.fullmatch(revision) is not None, "knowledge source requires an exact commit")
    entry = subprocess.run(["git", "-C", str(repository), "ls-tree", "-z", revision, "--", GRAPH],
                           capture_output=True, check=True).stdout
    if entry:
        require(entry.startswith(b"100644 blob ") and entry.endswith(("\t" + GRAPH + "\0").encode()),
                "knowledge graph is not a regular Git blob")
        return git_bytes(repository, revision, GRAPH)

    paths = ["docs", SOURCE_MANIFEST, *GENERATOR_DIGESTS]
    archive = bounded_archive(repository, revision, paths)
    with tempfile.TemporaryDirectory(prefix="pinned-okf-") as temporary:
        root = Path(temporary)
        seen = set()
        total = 0
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as source:
            for member in source:
                relative = member.name.rstrip("/")
                portable_path(relative)
                if member.isdir():
                    continue
                require(member.isfile() and relative not in seen, "unsafe knowledge source entry")
                seen.add(relative)
                if not (relative in GENERATOR_DIGESTS or relative == SOURCE_MANIFEST or
                        (relative.startswith("docs/") and relative.endswith((".md", ".json")))):
                    continue
                total += member.size
                require(total <= MAX_SOURCE_ARCHIVE_BYTES, "knowledge source files exceed bound")
                content = source.extractfile(member).read()
                if relative in GENERATOR_DIGESTS:
                    require(digest_bytes(content) == GENERATOR_DIGESTS[relative],
                            "unreviewed OKF graph generator")
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        require(set(GENERATOR_DIGESTS) | {SOURCE_MANIFEST} <= seen,
                "incomplete pinned OKF generator source")
        source_manifest = load_json(root / SOURCE_MANIFEST)
        require(isinstance(source_manifest, dict) and source_manifest.get("root") == "docs",
                "pinned OKF bundle root must be docs")
        output = root / GRAPH
        environment = {"PATH": os.defpath, "HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, "-E", "-s", str(root / "scripts/ci/generate-okf-graph.py"),
                                 "--output", str(output)], cwd=root, env=environment,
                                capture_output=True, text=True, timeout=30, check=False)
        require(result.returncode == 0 and output.is_file(),
                "pinned OKF graph generation failed: " + result.stdout[-500:] + result.stderr[-500:])
        return output.read_bytes()


def references(pack, graph):
    ids = {node["id"] for node in graph["nodes"]}
    for scenario in pack["scenarios"]:
        for field in ("conceptIds", "workflowIds"):
            require(bool(scenario.get(field)), f"{scenario['id']}: missing {field}")
            require(set(scenario[field]) <= ids, f"{scenario['id']}: unresolved OKF {field}")


def build(repository, revision, pack):
    graph_data = graph_bytes(repository, revision)
    graph = json.loads(graph_data)
    require(graph["profile"] == "honua.okf-agent-graph/v1" and graph["executionVerified"] is False,
            "unsupported or execution-claiming knowledge graph")
    references(pack, graph)
    files = {}
    documents = []
    for node in graph["nodes"]:
        relative = portable_path(node["path"])
        require(relative.endswith(".md") and node["id"] == relative[:-3], "invalid OKF identity")
        require(relative not in files, "duplicate OKF document")
        data = git_bytes(repository, revision, "docs/" + relative)
        require(digest_bytes(data) == node["documentDigest"], "stale OKF document digest")
        files[relative] = data
        documents.append({"id": node["id"], "digest": node["documentDigest"]})
    source_manifest = git_bytes(repository, revision, "scripts/ci/okf-bundle.v1.json")
    commitment = {"manifestDigest": digest_bytes(source_manifest), "documents": documents,
                  "interfaceBindings": graph["interfaceBindings"]}
    require(canonical_digest(commitment) == graph["bundleDigest"], "stale OKF bundle digest")
    files["okf/domain-graph.v1.json"] = graph_data
    manifest = {"schemaVersion": 1, "sourceRepository": REPOSITORY, "sourceRevision": revision,
                "bundleDigest": graph["bundleDigest"], "graphDigest": digest_bytes(graph_data),
                "normalization": "UTF-8/LF", "files": [
                    {"path": path, "sha256": digest_bytes(data), "bytes": len(data)}
                    for path, data in sorted(files.items())]}
    return manifest, files


def validate_binding(binding):
    require(isinstance(binding, dict) and set(binding) == {
        "sourceRepository", "sourceRevision", "bundleDigest", "graphDigest", "manifestDigest"},
        "missing or extra knowledge binding fields")
    require(binding["sourceRepository"] == REPOSITORY, "wrong knowledge repository")
    require(isinstance(binding["sourceRevision"], str) and SHA.fullmatch(binding["sourceRevision"]),
            "knowledge revision is not immutable")
    for key in ("bundleDigest", "graphDigest", "manifestDigest"):
        require(isinstance(binding[key], str) and DIGEST.fullmatch(binding[key]), "invalid knowledge digest")


def binding(manifest):
    require(isinstance(manifest, dict) and set(manifest) == {
        "schemaVersion", "sourceRepository", "sourceRevision", "bundleDigest", "graphDigest", "normalization", "files"},
        "invalid knowledge manifest fields")
    require(type(manifest["schemaVersion"]) is int and manifest["schemaVersion"] == 1
            and manifest["normalization"] == "UTF-8/LF", "unsupported knowledge manifest")
    require(isinstance(manifest["files"], list) and bool(manifest["files"]), "empty knowledge manifest")
    result = {key: manifest[key] for key in ("sourceRepository", "sourceRevision", "bundleDigest", "graphDigest")}
    result["manifestDigest"] = canonical_digest(manifest)
    validate_binding(result)
    return result


def verify(root, manifest, expected):
    validate_binding(expected)
    require(binding(manifest) == expected, "worker knowledge differs from frozen campaign")
    root = Path(root).resolve(strict=True)
    seen = set()
    for row in manifest["files"]:
        require(isinstance(row, dict) and set(row) == {"path", "sha256", "bytes"}
                and type(row["bytes"]) is int and row["bytes"] >= 0
                and isinstance(row["sha256"], str) and DIGEST.fullmatch(row["sha256"]),
                "invalid knowledge file descriptor")
        relative = portable_path(row["path"])
        require(relative not in seen, "duplicate knowledge file")
        seen.add(relative)
        path = root / relative
        require(path.is_file() and path.resolve().is_relative_to(root), "missing/escaping knowledge file")
        require(not any(p.is_symlink() for p in (path, *path.parents) if p != root and root in p.parents),
                "symlink knowledge file")
        data = path.read_bytes()
        require(len(data) == row["bytes"] and digest_bytes(data) == row["sha256"], "knowledge bytes changed")
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    require(actual == seen | {"knowledge-manifest.json"}, "unreviewed files in public knowledge export")
    graph = load_json(root / "okf/domain-graph.v1.json")
    require(graph.get("profile") == "honua.okf-agent-graph/v1" and graph.get("executionVerified") is False,
            "unsupported or execution-claiming knowledge graph")
    require(digest_bytes((root / "okf/domain-graph.v1.json").read_bytes()) == expected["graphDigest"],
            "knowledge graph changed")
    require(graph["bundleDigest"] == expected["bundleDigest"], "wrong graph bundle")
    documents = set()
    for node in graph["nodes"]:
        relative = portable_path(node["path"])
        require(relative.endswith(".md") and node["id"] == relative[:-3] and relative not in documents,
                "invalid or duplicate knowledge node")
        documents.add(relative)
        require(relative in seen and digest_bytes((root / relative).read_bytes()) == node["documentDigest"],
                "unbound knowledge node")
    require(seen == documents | {"okf/domain-graph.v1.json"}, "knowledge manifest differs from public graph")
    return graph


def export(repository, revision, pack, destination):
    manifest, files = build(repository, revision, pack)
    root = Path(destination)
    root.mkdir(parents=True, exist_ok=False)
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o444)
    (root / "knowledge-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    verify(root, manifest, binding(manifest))
    return binding(manifest)


def query(root, text, limit=8):
    manifest = load_json(Path(root) / "knowledge-manifest.json")
    graph = verify(root, manifest, binding(manifest))
    terms = set(re.findall(r"\w+", text.casefold()))
    ranked = []
    for node in graph["nodes"]:
        metadata = node["metadata"]
        words = " ".join([metadata.get("title", ""), metadata.get("description", ""), *metadata.get("aliases", [])])
        score = len(terms & set(re.findall(r"\w+", words.casefold())))
        if score:
            ranked.append((-score, node["id"], node))
    return {"knowledge": binding(manifest), "executionAuthorized": False, "nodes": [
        {"id": node["id"], "metadata": node["metadata"], "localDocument": node["path"],
         "humanDocument": f"{REPOSITORY}/blob/{manifest['sourceRevision']}/docs/{node['path']}"}
        for _, _, node in sorted(ranked)[:limit]]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repository", type=Path)
    p.add_argument("--revision")
    p.add_argument("--pack", type=Path)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--query")
    args = p.parse_args()
    if args.query is not None:
        result = query(args.root, args.query)
    else:
        p.error("export requires repository, revision and pack") if not all((args.repository, args.revision, args.pack)) else None
        result = export(args.repository, args.revision, load_json(args.pack), args.root)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
