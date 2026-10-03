#!/usr/bin/env python3
"""Inventory every fenced block of the public getting-started documents at their release revisions.

    python certification/executable-docs/inventory.py --write     # regenerate inventory.json
    python certification/executable-docs/inventory.py --check     # report drift from inventory.json

sources.json names the documents and the rule that selects each document's revision (the manifest's
client-artifact or component pin, the published npm package, or the default branch the public site
and samples are served from). This script never edits a document.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

import yaml

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))

from blocks import extract  # noqa: E402
from inputs import assigned_names, doc_id, needs  # noqa: E402

DEFAULT_SOURCES = HERE / "sources.json"
DEFAULT_INVENTORY = HERE / "inventory.json"
DELAYS = (0, 10, 30, 60, 120, 60)   # transient network errors retry for up to ~5 minutes


class InventoryError(RuntimeError):
    pass


def _get(url: str, *, accept: str | None = None, token: str | None = None) -> bytes:
    last: Exception | None = None
    for delay in DELAYS:
        time.sleep(delay)
        request = urllib.request.Request(url, headers={"User-Agent": "honua-executable-docs"})
        if accept:
            request.add_header("Accept", accept)
        if token and urllib.parse.urlparse(url).hostname in {"api.github.com", "raw.githubusercontent.com"}:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code in {404, 401}:
                raise InventoryError(f"GET {url}: HTTP {exc.code}") from exc
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = exc
    raise InventoryError(f"GET {url} failed after retries: {last}")


class Resolver:
    """Resolves a document's revision rule and reads the document bytes at that revision."""

    def __init__(self, manifest: dict[str, Any], token: str | None = None,
                 fetch: Callable[..., bytes] = _get, checkout: Path = ROOT):
        self.manifest = manifest
        self.token = token
        self.fetch = fetch
        self.checkout = checkout
        self._heads: dict[str, str] = {}

    def revision(self, document: dict[str, Any]) -> str:
        rule = document["revision"]
        if "clientArtifact" in rule:
            pin = self.manifest.get("clientArtifacts", {}).get(rule["clientArtifact"])
            if not pin or not pin.get("sourceSha"):
                raise InventoryError(f"{document['path']}: clientArtifacts.{rule['clientArtifact']}.sourceSha is unset")
            return str(pin["sourceSha"])
        if "component" in rule:
            sha = self.manifest.get("components", {}).get(rule["component"], {}).get("sha")
            if not sha:
                raise InventoryError(f"{document['path']}: components.{rule['component']}.sha is unset")
            return str(sha)
        if "npmPublished" in rule:
            name = urllib.parse.quote(rule["npmPublished"], safe="@")
            data = json.loads(self.fetch(f"https://registry.npmjs.org/{name}/latest"))
            if not data.get("gitHead"):
                raise InventoryError(f"npm {rule['npmPublished']}@latest records no gitHead")
            return str(data["gitHead"])
        if rule.get("defaultBranch"):
            repo = document["repo"]
            if repo not in self._heads:
                meta = json.loads(self.fetch(f"https://api.github.com/repos/{repo}", token=self.token))
                branch = urllib.parse.quote(meta["default_branch"], safe="")
                sha = self.fetch(f"https://api.github.com/repos/{repo}/commits/{branch}",
                                 accept="application/vnd.github.sha", token=self.token).decode().strip()
                self._heads[repo] = sha
            return self._heads[repo]
        if rule.get("checkout"):
            # The last commit that changed the document: stable across unrelated commits.
            sha = subprocess.run(["git", "-C", str(self.checkout), "log", "-1", "--format=%H", "--",
                                  document["path"]], check=True, capture_output=True, text=True).stdout.strip()
            if not sha:
                raise InventoryError(f"{document['path']}: not tracked in this checkout")
            return sha
        raise InventoryError(f"{document['path']}: unknown revision rule {rule}")

    def read(self, document: dict[str, Any], revision: str) -> str:
        if document["revision"].get("checkout"):
            return (self.checkout / document["path"]).read_text(encoding="utf-8")
        url = f"https://raw.githubusercontent.com/{document['repo']}/{revision}/{document['path']}"
        return self.fetch(url, token=self.token).decode("utf-8")


def document_record(document: dict[str, Any], revision: str, text: str,
                    defined_before: set[str] | None = None) -> tuple[dict[str, Any], set[str]]:
    fmt = document.get("format", "markdown")
    blocks = extract(text, "html" if fmt == "html" else "markdown")
    defined = set(defined_before or ())
    rows = []
    for block in blocks:
        row = block.record()
        if block.intent in {"run", "compile", "file"}:
            need = needs(block, defined)
            if any(need.values()):
                row["needs"] = {k: v for k, v in need.items() if v}
        defined |= assigned_names(block)
        rows.append(row)
    host = "github.com"
    record = {
        "id": doc_id(document["repo"], document["path"]),
        "repo": document["repo"],
        "path": document["path"],
        "revision": revision,
        "revisionRule": document["revision"],
        "url": f"https://{host}/{document['repo']}/blob/{revision}/{document['path']}",
        "runtime": document["runtime"],
        "docker": bool(document.get("docker")),
        "session": document.get("session"),
        "checkout": document.get("checkout"),
        "blocks": rows,
    }
    return record, defined


def build(sources: dict[str, Any], resolver: Resolver) -> dict[str, Any]:
    documents = []
    session_env: dict[str, set[str]] = {}
    for document in sources["documents"]:
        revision = resolver.revision(document)
        text = resolver.read(document, revision)
        session = document.get("session")
        record, defined = document_record(document, revision, text, session_env.get(session) if session else None)
        if session:
            session_env[session] = defined
        documents.append(record)
    counts: dict[str, int] = {}
    for record in documents:
        for row in record["blocks"]:
            counts[row["intent"]] = counts.get(row["intent"], 0) + 1
    return {
        "schemaVersion": 1,
        "platformRelease": resolver.manifest.get("platformRelease"),
        "documents": documents,
        "outOfScope": sources.get("outOfScope", []),
        "summary": {"documents": len(documents), "blocks": sum(counts.values()),
                    "byIntent": dict(sorted(counts.items()))},
    }


def drift(committed: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Human-readable differences between the committed inventory and the live documents."""
    out = []
    old = {d["id"]: d for d in committed.get("documents", [])}
    for doc in current["documents"]:
        prior = old.pop(doc["id"], None)
        if prior is None:
            out.append(f"{doc['id']}: not in the committed inventory")
            continue
        if prior["revision"] != doc["revision"]:
            out.append(f"{doc['id']}: revision {prior['revision'][:12]} -> {doc['revision'][:12]}")
        before = [(b["sha256"], b["intent"]) for b in prior["blocks"]]
        after = [(b["sha256"], b["intent"]) for b in doc["blocks"]]
        if before != after:
            out.append(f"{doc['id']}: blocks changed ({len(before)} -> {len(after)})")
    out.extend(f"{name}: no longer a declared document" for name in old)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--manifest", type=Path, default=ROOT / "platform-manifest.yaml")
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate the committed inventory")
    mode.add_argument("--check", action="store_true", help="print drift; exit 1 when the inventory is stale")
    mode.add_argument("--print", dest="print_", action="store_true", help="print the live inventory")
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    try:
        current = build(sources, Resolver(manifest, token=token))
    except InventoryError as exc:
        print(f"inventory error: {exc}", file=sys.stderr)
        return 2
    text = json.dumps(current, indent=2) + "\n"
    if args.print_:
        print(text, end="")
        return 0
    if args.write:
        args.inventory.write_text(text, encoding="utf-8")
        print(f"wrote {args.inventory}: {current['summary']}")
        return 0
    committed = json.loads(args.inventory.read_text(encoding="utf-8"))
    changes = drift(committed, current)
    for line in changes:
        print(line)
    return 1 if changes else 0


if __name__ == "__main__":
    raise SystemExit(main())
