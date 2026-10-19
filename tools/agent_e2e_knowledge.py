"""Pin and export public OKF documents without exporting a repository or test answers.

Git objects, rather than working-tree files, supply the bytes. This module does not
execute code from the documentation repository. The controller authenticates the
source revision; a content hash alone is not an attestation.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess

from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, portable_path

REPOSITORY = "https://github.com/honua-io/honua-server"
GRAPH = "docs/okf/domain-graph.v1.json"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def require(value, message):
    if not value:
        raise PackError(message)


def git_bytes(repository, revision, path):
    require(SHA.fullmatch(revision) is not None, "knowledge source requires an exact commit")
    portable_path(path)
    result = subprocess.run(["git", "-C", str(repository), "show", f"{revision}:{path}"],
                            capture_output=True, check=True)
    return result.stdout.decode("utf-8").replace("\r\n", "\n").encode("utf-8")


def references(pack, graph):
    ids = {node["id"] for node in graph["nodes"]}
    for scenario in pack["scenarios"]:
        for field in ("conceptIds", "workflowIds"):
            require(bool(scenario.get(field)), f"{scenario['id']}: missing {field}")
            require(set(scenario[field]) <= ids, f"{scenario['id']}: unresolved OKF {field}")


def build(repository, revision, pack):
    graph_bytes = git_bytes(repository, revision, GRAPH)
    graph = json.loads(graph_bytes)
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
    files["okf/domain-graph.v1.json"] = graph_bytes
    manifest = {"schemaVersion": 1, "sourceRepository": REPOSITORY, "sourceRevision": revision,
                "bundleDigest": graph["bundleDigest"], "graphDigest": digest_bytes(graph_bytes),
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
