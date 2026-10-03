#!/usr/bin/env python3
"""Classify and lint third-party trademark terms (Esri, ArcGIS and Esri product names) — honua-release#425.

Every occurrence in a checkout's text files is put in one of the three classes defined by
docs/THIRD-PARTY-TRADEMARKS.md:

  spec        an identifier from tools/vendor-terms/geoservices-identifiers.v1.json — the GeoServices REST
              Specification vocabulary a client sends or expects verbatim (esriGeometryPoint, ...)
  nominative  a compatibility statement in prose (docs, site, compatibility matrix) in a file that carries
              the required attribution, with no endorsement language and not leading a heading
  avoidable   everything else: our own identifiers, repository/package/namespace/path/test names,
              comments, copy, and unattributed compatibility statements

Subcommands:

  scan              per-repo report (counts by class, every avoidable hit with path:line), JSON + Markdown
  lint              fail when a file's avoidable hits exceed the committed baseline for that repo
  baseline          write the baseline for a repo from its current avoidable hits
  check-baselines   fail when a committed baseline grew relative to a base ref (baselines only shrink)

A scan reads either a working tree (tracked plus untracked-but-not-ignored files, so .gitignore is
respected) or, with --git-ref, the blobs of a commit without checking it out.
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, field
import functools
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
TERMS_DIR = ROOT / "tools" / "vendor-terms"
VOCABULARY = TERMS_DIR / "geoservices-identifiers.v1.json"
ALLOWLIST = TERMS_DIR / "allowlist.json"
REPORT_SCHEMA = "honua.vendor-terms.report.v1"
BASELINE_SCHEMA = "honua.vendor-terms.baseline.v1"
ALLOWLIST_SCHEMA = "honua.vendor-terms.allowlist.v1"

CLASSES = ("spec", "nominative", "avoidable")

# A token is the identifier-like run around a mark; ``EsriFeatureLayer`` is one hit, not two.
MARK = re.compile(r"esri|arcgis", re.IGNORECASE)
IDENTIFIER_CHAR = re.compile(r"[A-Za-z0-9_]")
# Esri product names, longest first so "ArcGIS Maps SDK for JavaScript" wins over "ArcGIS".
PRODUCT_NAMES = (
    "ArcGIS Maps SDK for JavaScript", "ArcGIS API for JavaScript", "ArcGIS Maps SDK for .NET",
    "ArcGIS Maps SDK for Qt", "ArcGIS Maps SDK for Swift", "ArcGIS Maps SDK for Kotlin",
    "ArcGIS Runtime SDK", "ArcGIS Living Atlas", "ArcGIS Maps SDK", "ArcGIS JS API", "ArcGIS Pro",
    "ArcGIS Online", "ArcGIS Enterprise", "Portal for ArcGIS", "ArcGIS Server", "ArcGIS Desktop",
    "ArcGIS Experience Builder", "ArcGIS Dashboards", "ArcGIS Field Maps", "ArcGIS Survey123",
    "ArcGIS StoryMaps", "ArcGIS Hub", "ArcGIS Runtime", "Esri Leaflet", "Esri Shapefile",
)
PRODUCT = re.compile("|".join(re.escape(name).replace(r"\ ", r"\s+") for name in PRODUCT_NAMES),
                     re.IGNORECASE)
# Esri marks that do not contain either substring.
STANDALONE = re.compile(r"\b(?:Living\s+Atlas|ArcMap|ArcCatalog|ArcPy|ArcObjects|ArcSDE|ArcIMS)\b",
                        re.IGNORECASE)
BARE_MARKS = {"esri", "arcgis"}
CANONICAL_PRODUCT = {name.lower(): name for name in PRODUCT_NAMES}
PRESCREEN = re.compile(r"esri|arcgis|living\s+atlas|arcmap|arccatalog|arcpy|arcobjects|arcsde|arcims",
                       re.IGNORECASE)

# The two clauses of the required attribution (docs/THIRD-PARTY-TRADEMARKS.md), whitespace-insensitive.
ATTRIBUTION_CLAUSES = (
    re.compile(r"are\s+trademarks,\s+registered\s+trademarks,\s+or\s+service\s+marks\s+of\s+Esri",
               re.IGNORECASE),
    re.compile(r"not\s+affiliated\s+with,\s+sponsored\s+by,\s+or\s+endorsed\s+by\s+Esri", re.IGNORECASE),
)
ATTRIBUTION_LINE = re.compile(r"trademark|service\s+mark|endorsed\s+by\s+Esri|affiliated\s+with",
                              re.IGNORECASE)
COMPATIBILITY = re.compile(
    r"\b(?:works?\s+with|working\s+with|compatib\w*|interoperab\w*|interoperates?\s+with|"
    r"tested\s+(?:with|against|in)|verified\s+(?:with|against|in)|validated\s+(?:with|against|in)|"
    r"connects?\s+(?:from|to|with)|connecting\s+(?:from|to|with)|clients?\s+(?:such\s+as|including|like)|"
    r"for\s+use\s+with|supports?|supported\s+(?:by|in|with)|opens?\s+in|loads?\s+in|consumed\s+by|"
    r"from\s+within)\b", re.IGNORECASE)
ENDORSEMENT = re.compile(r"\b(?:certified\s+by|endorsed\s+by|approved\s+by|official|partner\w*|"
                         r"powered\s+by|sponsored\s+by|affiliated\s+with)\b", re.IGNORECASE)
HEADING_LEADS_WITH_MARK = re.compile(r"^\s*#{1,6}\s*[*_`]*\s*(?:esri|arcgis)", re.IGNORECASE)
# Headings in the other prose formats: HTML <h1>-<h6>/<title>, AsciiDoc "= Title", and a line underlined by
# the next one (reStructuredText, AsciiDoc two-line and Markdown setext titles).
HTML_HEADING = re.compile(r"^\s*<(?:h[1-6]|title)\b", re.IGNORECASE)
ASCIIDOC_HEADING = re.compile(r"^\s*={1,6}\s+\S")
UNDERLINE = re.compile(r"^\s*([=\-~^\"'`#*+.:_])\1{2,}\s*$")
LEADS_WITH_MARK = re.compile(r"^[\s*_`#=]*(?:esri|arcgis|living\s+atlas|arcmap|arccatalog|arcpy|arcobjects|"
                             r"arcsde|arcims)", re.IGNORECASE)

PROSE_SUFFIXES = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt", ".html", ".htm"}
# A compatibility matrix that is prose (a Markdown or HTML page) exempts only its table rows.
TABLE_ROW = re.compile(r"^\s*\||<t[dh]\b", re.IGNORECASE)
MATRIX_PATH = re.compile(r"(?:^|/)(?:[^/]*compatib[^/]*|[^/]*matrix[^/]*)(?:/|$)", re.IGNORECASE)
TEST_PATH = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|e2e|testing)(?:/|$)|(?:^|/)test_[^/]*$|"
                       r"(?:_test|\.test|\.spec|Tests?)\.[A-Za-z]+$|\.Tests?(?:/|\.)", re.IGNORECASE)
TEST_DECLARATION = re.compile(
    r"^\s*(?:async\s+)?(?:def\s+test|class\s|def\s|func\s+Test|fn\s|"
    r"(?:public|private|internal|protected)\s+(?:static\s+)?(?:async\s+)?(?:void|Task|class|sealed|record)\b|"
    r"(?:describe|it|test|context|suite)(?:\.\w+)?\s*\(|\[(?:Fact|Theory|Test|TestMethod)\b)")
NAMESPACE_LINE = re.compile(r"^\s*(?:namespace|using|import|from|package|module|require|export\s+\*\s+from)\b|"
                            r"\brequire\(|\bimport\(|^\s*<(?:RootNamespace|AssemblyName)>")
PACKAGE_DECLARATION = re.compile(
    r"^\s*\"name\"\s*:|^\s*name\s*=|<(?:PackageId|AssemblyName|RootNamespace|Product|Title)>|"
    r"\bname\s*=\s*['\"]|^\s*module\s+\S", re.IGNORECASE)
PACKAGE_MANIFEST = re.compile(r"(?:^|/)(?:package\.json|pyproject\.toml|setup\.py|setup\.cfg|go\.mod|"
                              r"[^/]+\.(?:csproj|fsproj|vbproj|nuspec)|Directory\.Build\.props|Cargo\.toml)$")
REPO_NAME = re.compile(r"honua-esri-compat|esri-compat", re.IGNORECASE)
# A Honua-owned repository, package, module or namespace carrying a mark: honua-esri-assess, Honua.Esri.*
HONUA_NAME = re.compile(r"honua[-_./]?(?:[A-Za-z0-9]+[-_./])*(?:esri|arcgis)", re.IGNORECASE)
# Third-party packages and modules we depend on or drive by their published names.
THIRD_PARTY_WORD = re.compile(
    r"^\W*(?:@esri/|@arcgis/|esri-leaflet|esri-loader|arcgis-rest-|arcgis-js-api|Esri\.ArcGISRuntime|"
    r"L\.esri\.|arcpy\.|arcgis\.(?:gis|features|geometry|mapping|raster|network|learn)\b)", re.IGNORECASE)
THIRD_PARTY_IMPORT = re.compile(r"^\s*(?:import\s+(?:arcpy|arcgis)\b|from\s+(?:arcpy|arcgis)(?:\.\w+)*\s+import\b)")
URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+|\b[\w.-]+\.(?:esri|arcgis)\.com\b|"
                 r"\b(?:esri|arcgis)\.com\b", re.IGNORECASE)
ENCODED = re.compile(r"[A-Za-z0-9+/=]{80,}")

SLASH_COMMENT = {".cs", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".go", ".kt", ".kts",
                 ".swift", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs", ".scss", ".less", ".proto", ".fs",
                 ".dart", ".groovy", ".gradle", ".css", ".jsonc", ".vue", ".svelte", ".astro", ".razor"}
HASH_COMMENT = {".py", ".sh", ".bash", ".yml", ".yaml", ".toml", ".rb", ".ps1", ".psm1", ".r", ".tf",
                ".hcl", ".cfg", ".ini", ".conf", ".properties", ".mk", ".env", ".dockerfile", ".gitignore",
                ".gitattributes", ".editorconfig", ".dockerignore", ".bicep"}
HASH_COMMENT_NAMES = {"Dockerfile", "Makefile", "CODEOWNERS", ".gitignore", ".gitattributes",
                      ".dockerignore", ".editorconfig"}
SQL_COMMENT = {".sql"}
MARKUP_COMMENT = {".xml", ".csproj", ".props", ".targets", ".nuspec", ".svg", ".xaml", ".resx",
                  ".config", ".html", ".htm", ".md", ".mdx", ".markdown", ".vue", ".svelte", ".astro",
                  ".razor", ".cshtml", ".xsd", ".wsdl", ".gml", ".kml", ".sld", ".xsl", ".xslt"}

# Generated or vendored files that restate what a scanned manifest already declares.
GENERATED = re.compile(r"(?:^|/)(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|"
                       r"poetry\.lock|Pipfile\.lock|uv\.lock|packages\.lock\.json|Cargo\.lock|go\.sum)$|"
                       r"\.min\.(?:js|css)$|\.map$")
BUNDLE_SUFFIXES = {".js", ".mjs", ".cjs", ".css"}


# --------------------------------------------------------------------------------------------- inputs

def load_vocabulary(path: Path = VOCABULARY) -> dict[str, dict]:
    document = json.loads(path.read_text(encoding="utf-8"))
    return {entry["identifier"]: entry for entry in document["identifiers"]}


@dataclass(frozen=True)
class Exception_:
    """One allowlist entry: reviewed avoidable uses that do not count against the lint."""

    index: int
    repo: str
    paths: tuple[str, ...]
    tokens: tuple[str, ...]
    reason: str
    owner: str

    def covers(self, repo: str, path: str, token: str) -> bool:
        if self.repo not in (repo, "*"):
            return False
        if not any(glob_match(pattern, path) for pattern in self.paths):
            return False
        return not self.tokens or token.lower() in {t.lower() for t in self.tokens}


def load_allowlist(path: Path | None) -> list[Exception_]:
    if path is None or not path.is_file():
        return []
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != ALLOWLIST_SCHEMA:
        raise SystemExit(f"{path}: schema must be {ALLOWLIST_SCHEMA}")
    entries = []
    for index, raw in enumerate(document.get("entries", [])):
        missing = [key for key in ("repo", "paths", "reason", "owner") if not raw.get(key)]
        if missing:
            raise SystemExit(f"{path}: entry {index} lacks {', '.join(missing)} — every exception needs "
                             "a reason and an owner")
        entries.append(Exception_(index, raw["repo"], tuple(raw["paths"]), tuple(raw.get("tokens", ())),
                                  raw["reason"], raw["owner"]))
    return entries


def glob_match(pattern: str, path: str) -> bool:
    """fnmatch with ``**`` spanning directories and ``*`` stopping at ``/``."""
    return _glob_regex(pattern).fullmatch(path) is not None


@functools.lru_cache(maxsize=None)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    regex = ""
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            regex += "(?:.*/)?"
            index += 3
        elif pattern.startswith("**", index):
            regex += ".*"
            index += 2
        elif pattern[index] == "*":
            regex += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            regex += "[^/]"
            index += 1
        else:
            regex += re.escape(pattern[index])
            index += 1
    return re.compile(regex)


# --------------------------------------------------------------------------------------------- sources

def _git(root: Path, *args: str, binary: bool = False):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True)
    return result.stdout if binary else result.stdout.decode()


def working_tree_files(root: Path) -> Iterator[tuple[str, bytes | None]]:
    """Tracked plus untracked-but-not-ignored files; plain directory walk outside a git checkout.

    A symlink yields its own path with ``None`` content: its name is classified, its target is not followed.
    """
    try:
        listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                      binary=True).split(b"\0")
        paths = sorted({item.decode("utf-8", "surrogateescape") for item in listed if item})
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = sorted(str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*")
                       if (p.is_file() or p.is_symlink()) and ".git" not in p.relative_to(root).parts)
    for relative in paths:
        full = root / relative
        if full.is_symlink():
            yield relative, None
        elif full.is_file():
            yield relative, full.read_bytes()


def git_ref_files(root: Path, ref: str) -> Iterator[tuple[str, bytes | None]]:
    """Blobs of ``ref`` (tracked content only), read through one ``git cat-file --batch``; symlinks as in
    ``working_tree_files``."""
    entries = []
    links = []
    for record in _git(root, "ls-tree", "-r", "-z", "--full-tree", ref, binary=True).split(b"\0"):
        if not record:
            continue
        meta, _, name = record.partition(b"\t")
        mode, kind, sha = meta.decode().split()
        if kind == "blob" and mode == "120000":
            links.append(name.decode("utf-8", "surrogateescape"))
        elif kind == "blob":
            entries.append((name.decode("utf-8", "surrogateescape"), sha))
    yield from ((path, None) for path in sorted(links))
    process = subprocess.Popen(["git", "-C", str(root), "cat-file", "--batch"], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE)
    assert process.stdin and process.stdout
    try:
        for path, sha in sorted(entries):
            process.stdin.write(sha.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().split()
            size = int(header[2])
            data = process.stdout.read(size)
            process.stdout.read(1)
            yield path, data
    finally:
        process.stdin.close()
        process.wait()


# --------------------------------------------------------------------------------------------- classify

@dataclass
class Hit:
    path: str
    line: int
    token: str
    term: str
    cls: str
    category: str
    exception: int | None = None

    def as_dict(self) -> dict:
        data = {"path": self.path, "line": self.line, "token": self.token, "term": self.term,
                "class": self.cls, "category": self.category}
        if self.exception is not None:
            data["exception"] = self.exception
        return data


@dataclass
class FileContext:
    path: str
    text: str
    suffix: str = field(init=False)
    prose: bool = field(init=False)
    matrix: bool = field(init=False)
    test: bool = field(init=False)
    manifest: bool = field(init=False)
    attributed: bool = field(init=False)

    def __post_init__(self) -> None:
        name = PurePosixPath(self.path).name
        self.suffix = PurePosixPath(self.path).suffix.lower() or ("." + name.lower() if name.startswith(".")
                                                                  else "")
        if name in HASH_COMMENT_NAMES:
            self.suffix = self.suffix or ".dockerfile"
        self.prose = self.suffix in PROSE_SUFFIXES
        self.matrix = MATRIX_PATH.search(self.path) is not None
        self.test = TEST_PATH.search(self.path) is not None
        self.manifest = PACKAGE_MANIFEST.search(self.path) is not None
        self.attributed = is_attributed(self.text)


def is_attributed(text: str) -> bool:
    flattened = re.sub(r"(?m)^\s*(?:#+|//+|\*|>)\s?", " ", text)
    flattened = re.sub(r"[*_`]", "", flattened)
    return all(clause.search(flattened) for clause in ATTRIBUTION_CLAUSES)


def _heading_leads_with_mark(context: FileContext, line: str, next_line: str) -> bool:
    """A heading or page title that starts with a mark, in any prose format (Markdown ``#`` everywhere)."""
    if HEADING_LEADS_WITH_MARK.search(line):
        return True
    if not context.prose:
        return False
    if HTML_HEADING.search(line):
        return LEADS_WITH_MARK.search(re.sub(r"<[^>]*>", "", line)) is not None
    if ASCIIDOC_HEADING.search(line) or (line.strip() and UNDERLINE.match(next_line)):
        return LEADS_WITH_MARK.search(line) is not None
    return False


def _comment_kind(context: FileContext) -> str | None:
    name = PurePosixPath(context.path).name
    if context.suffix in SLASH_COMMENT:
        return "slash"
    if context.suffix in HASH_COMMENT or name in HASH_COMMENT_NAMES:
        return "hash"
    if context.suffix in SQL_COMMENT:
        return "sql"
    if context.suffix in MARKUP_COMMENT:
        return "markup"
    return None


def _outside_quotes(line: str, index: int) -> bool:
    return line.count('"', 0, index) % 2 == 0 and line.count("'", 0, index) % 2 == 0


def _comment_start(line: str, kind: str | None) -> int | None:
    markers = {"slash": ("//", "/*"), "hash": ("#",), "sql": ("--",), "markup": ("<!--",)}.get(kind or "", ())
    best = None
    for marker in markers:
        start = 0
        while (found := line.find(marker, start)) != -1:
            # ``://`` is a URL, not a comment
            if marker == "//" and found > 0 and line[found - 1] == ":":
                start = found + 2
                continue
            if _outside_quotes(line, found):
                best = found if best is None else min(best, found)
                break
            start = found + len(marker)
    return best


def _inside_quotes(line: str, index: int) -> bool:
    return not _outside_quotes(line, index) or line.count("`", 0, index) % 2 == 1


def _word_around(line: str, start: int, end: int) -> str:
    left = start
    while left > 0 and not line[left - 1].isspace() and line[left - 1] not in "\"'()[]{}<>,;|":
        left -= 1
    right = end
    while right < len(line) and not line[right].isspace() and line[right] not in "\"'()[]{}<>,;|":
        right += 1
    return line[left:right]


def _spans(line: str) -> list[tuple[int, int, str, str]]:
    """(start, end, token, term) for every mark on a line; product names absorb their tokens."""
    spans: list[tuple[int, int, str, str]] = []
    taken: list[tuple[int, int]] = []
    for match in PRODUCT.finditer(line):
        # "ArcGIS ProSomething" is an identifier, not the product name
        if match.end() < len(line) and (line[match.end()].isalnum() or line[match.end()] == "_"):
            continue
        canonical = CANONICAL_PRODUCT[re.sub(r"\s+", " ", match.group()).lower()]
        spans.append((match.start(), match.end(), match.group(), canonical))
        taken.append((match.start(), match.end()))
    for match in STANDALONE.finditer(line):
        if any(s <= match.start() < e for s, e in taken):
            continue
        spans.append((match.start(), match.end(), match.group(), re.sub(r"\s+", " ", match.group())))
        taken.append((match.start(), match.end()))
    for start, end in _mark_tokens(line):
        if any(s <= start < e for s, e in taken):
            continue
        token = line[start:end]
        lowered = token.lower()
        term = "esri" if "esri" in lowered and ("arcgis" not in lowered or
                                                 lowered.index("esri") < lowered.index("arcgis")) else "arcgis"
        spans.append((start, end, token, term))
    return sorted(spans)


def _starts_word(line: str, index: int) -> bool:
    """A mark counts where a word starts: ``SourceSrid`` and ``Desire`` hide no mark, ``isEsri`` does."""
    if index == 0 or not line[index - 1].isalpha():
        return True
    return line[index].isupper()


def _mark_tokens(line: str) -> list[tuple[int, int]]:
    tokens: list[tuple[int, int]] = []
    for match in MARK.finditer(line):
        if not _starts_word(line, match.start()):
            continue
        start = match.start()
        while start > 0 and IDENTIFIER_CHAR.match(line[start - 1]):
            start -= 1
        end = match.end()
        while end < len(line) and IDENTIFIER_CHAR.match(line[end]):
            end += 1
        if not tokens or tokens[-1] != (start, end):
            tokens.append((start, end))
    return tokens


class Classifier:
    def __init__(self, repo: str, vocabulary: dict[str, dict], allowlist: list[Exception_]):
        self.repo = repo
        self.vocabulary = vocabulary
        self.allowlist = allowlist
        self.skipped: Counter[str] = Counter()

    def _exception(self, path: str, token: str) -> int | None:
        for entry in self.allowlist:
            if entry.covers(self.repo, path, token):
                return entry.index
        return None

    def classify_path(self, path: str) -> list[Hit]:
        """One hit per path prefix whose final component carries a mark (a directory counts once)."""
        hits = []
        parts = path.split("/")
        for depth, part in enumerate(parts):
            for start, end, token, term in _spans(part):
                prefix = "/".join(parts[:depth + 1])
                hits.append(Hit(prefix, 0, token, term, "avoidable", "path", self._exception(prefix, token)))
        return hits

    def classify_text(self, path: str, text: str) -> list[Hit]:
        if not PRESCREEN.search(text):
            return []
        context = FileContext(path, text)
        kind = _comment_kind(context)
        hits: list[Hit] = []
        in_fence = False
        in_block = False  # /* */ or <!-- --> spanning lines
        lines = text.splitlines()
        for number, line in enumerate(lines, start=1):
            stripped = line.strip()
            if context.prose and stripped.startswith(("```", "~~~")):
                in_fence = not in_fence
                continue
            if not in_block and not PRESCREEN.search(line) and "/*" not in line and "<!--" not in line:
                continue
            block_open = in_block
            if kind in ("slash", "markup"):
                opener, closer = ("/*", "*/") if kind == "slash" else ("<!--", "-->")
                if in_block:
                    if closer in line:
                        in_block = False
                else:
                    position = line.rfind(opener)
                    if position != -1 and closer not in line[position:] and _outside_quotes(line, position):
                        in_block = True
            spans = _spans(line)
            if not spans:
                continue
            comment_at = _comment_start(line, kind)
            for start, end, token, term in spans:
                if ENCODED.fullmatch(_word_around(line, start, end)):
                    self.skipped["encoded-token"] += 1
                    continue
                heading = _heading_leads_with_mark(context, line, lines[number] if number < len(lines) else "")
                cls, category = self._classify(context, line, start, end, token, in_fence, block_open,
                                               comment_at, heading)
                exception = self._exception(path, token) if cls == "avoidable" else None
                hits.append(Hit(path, number, token, term, cls, category, exception))
        return hits

    def _classify(self, context: FileContext, line: str, start: int, end: int, token: str,
                  in_fence: bool, block_open: bool, comment_at: int | None,
                  heading: bool = False) -> tuple[str, str]:
        if token in self.vocabulary:
            return "spec", "spec-identifier"
        word = _word_around(line, start, end)
        stripped = line.strip()
        in_comment = (block_open or (comment_at is not None and start > comment_at)
                      or stripped.startswith(("*", "/*", "///", "<!--")) and context.suffix in SLASH_COMMENT)
        prose_line = context.prose and not in_fence and not in_comment
        if self._nominative(context, line, start, token, word, prose_line, heading):
            return "nominative", ("attribution" if ATTRIBUTION_LINE.search(line) else "compatibility-statement")
        if REPO_NAME.search(word) or HONUA_NAME.search(word) or (context.manifest
                                                                  and PACKAGE_DECLARATION.search(line)):
            if NAMESPACE_LINE.search(line) and not context.prose:
                return "avoidable", "namespace-or-import"
            return "avoidable", "repo-or-package-name"
        if THIRD_PARTY_WORD.search(word) or (THIRD_PARTY_IMPORT.search(line)
                                             and token.lower() in {"arcpy", "arcgis"}):
            return "avoidable", "third-party-reference"
        if URL.search(word):
            return "avoidable", "url"
        if in_comment:
            return "avoidable", "comment"
        if context.prose and not in_fence and not line.count("`", 0, start) % 2:
            return "avoidable", "docs-copy"
        if NAMESPACE_LINE.search(line):
            return "avoidable", "namespace-or-import"
        if context.test and TEST_DECLARATION.search(line):
            return "avoidable", "test-name"
        if _inside_quotes(line, start):
            # a product named as a data label (a client under test) rather than in our own identifiers
            product = token in PRODUCT_NAMES or PRODUCT.fullmatch(token) or STANDALONE.fullmatch(token)
            return "avoidable", "product-name-label" if product else "string-literal"
        return "avoidable", "identifier"

    @staticmethod
    def _nominative(context: FileContext, line: str, start: int, token: str, word: str,
                    prose_line: bool, heading: bool = False) -> bool:
        if not (prose_line or context.matrix) or not context.attributed:
            return False
        if token.lower() not in BARE_MARKS and token not in PRODUCT_NAMES and not PRODUCT.fullmatch(token) \
                and not STANDALONE.fullmatch(token):
            return False  # compound identifiers (EsriFeatureLayer, esri-compat) are never nominative
        if word != token and not re.fullmatch(r"[\W_]*" + re.escape(token) + r"(?:'s|’s)?[\W_]*", word):
            return False  # part of a path, URL, package or identifier
        if line.count("`", 0, start) % 2:
            return False  # inline code is code, not prose
        if ATTRIBUTION_LINE.search(line):
            return True  # the attribution notice itself
        if heading or ENDORSEMENT.search(line):
            return False
        # a structured matrix (YAML/JSON rows) or a matrix page's table row; any other line needs the
        # compatibility language
        matrix_row = context.matrix and (not context.prose or TABLE_ROW.search(line) is not None)
        return matrix_row or COMPATIBILITY.search(line) is not None


# --------------------------------------------------------------------------------------------- scan

def scan(files: Iterable[tuple[str, bytes | None]], repo: str, vocabulary: dict[str, dict],
         allowlist: list[Exception_]) -> tuple[list[Hit], Counter[str], int]:
    classifier = Classifier(repo, vocabulary, allowlist)
    hits: list[Hit] = []
    seen_prefixes: set[str] = set()
    scanned = 0
    for path, data in files:
        # every mark in a path component is a hit, but a directory shared by many files counts once
        path_hits = classifier.classify_path(path)
        hits += [hit for hit in path_hits if hit.path not in seen_prefixes]
        seen_prefixes.update(hit.path for hit in path_hits)
        if data is None:
            classifier.skipped["symlink"] += 1
            continue
        if GENERATED.search(path):
            classifier.skipped["generated"] += 1
            continue
        if b"\0" in data[:8192]:
            classifier.skipped["binary"] += 1
            continue
        if _minified(path, data):
            classifier.skipped["minified"] += 1
            continue
        scanned += 1
        hits.extend(classifier.classify_text(path, data.decode("utf-8", errors="replace")))
    return hits, classifier.skipped, scanned


def _minified(path: str, data: bytes) -> bool:
    """Bundled build output (a few enormous lines) restates its sources, which are scanned themselves."""
    if PurePosixPath(path).suffix.lower() not in BUNDLE_SUFFIXES or len(data) < 4096:
        return False
    return len(data) / (data.count(b"\n") + 1) > 1000


def build_report(repo: str, sha: str, hits: list[Hit], skipped: Counter[str], scanned: int,
                 vocabulary_path: Path = VOCABULARY) -> dict:
    counts = Counter(hit.cls for hit in hits)
    avoidable = [hit for hit in hits if hit.cls == "avoidable"]
    open_hits = [hit for hit in avoidable if hit.exception is None]
    return {
        "schema": REPORT_SCHEMA,
        "repo": repo,
        "sha": sha,
        "vocabulary": {"path": "tools/vendor-terms/geoservices-identifiers.v1.json",
                       "sha256": hashlib.sha256(vocabulary_path.read_bytes()).hexdigest()},
        "files": {"scanned": scanned, "skipped": dict(sorted(skipped.items()))},
        "counts": {cls: counts.get(cls, 0) for cls in CLASSES} | {
            "avoidableExcepted": len(avoidable) - len(open_hits)},
        "byCategory": {cls: dict(Counter(hit.category for hit in hits if hit.cls == cls).most_common())
                       for cls in CLASSES},
        "byTerm": {cls: dict(Counter(hit.term for hit in hits if hit.cls == cls).most_common())
                   for cls in CLASSES},
        "topSpecIdentifiers": dict(Counter(hit.token for hit in hits if hit.cls == "spec").most_common(25)),
        "topAvoidableTokens": dict(Counter(hit.token for hit in open_hits).most_common(25)),
        "avoidableByFile": dict(sorted(Counter(hit.path for hit in open_hits).items())),
        "avoidable": [hit.as_dict() for hit in avoidable],
    }


def render_markdown(report: dict) -> str:
    counts = report["counts"]
    lines = [
        f"# Vendor-term inventory: {report['repo']} @ {report['sha'][:12]}",
        "",
        "Generated by `tools/check_vendor_terms.py scan` (honua-release#425); classes are defined in "
        "[docs/THIRD-PARTY-TRADEMARKS.md](../../../docs/THIRD-PARTY-TRADEMARKS.md).",
        "",
        f"Commit `{report['sha']}` — {report['files']['scanned']} text files scanned"
        + (", skipped: " + ", ".join(f"{n} {k}" for k, n in report["files"]["skipped"].items())
           if report["files"]["skipped"] else "") + ".",
        "",
        "| Class | Hits |",
        "| --- | ---: |",
        f"| spec | {counts['spec']} |",
        f"| nominative | {counts['nominative']} |",
        f"| avoidable | {counts['avoidable']} |",
        f"| avoidable, excepted by allowlist | {counts['avoidableExcepted']} |",
        "",
        "## Avoidable hits by category",
        "",
        "| Category | Hits |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {n} |" for name, n in report["byCategory"]["avoidable"].items()]
    lines += ["", "## Avoidable hits by term", "", "| Term | Hits |", "| --- | ---: |"]
    lines += [f"| {name} | {n} |" for name, n in report["byTerm"]["avoidable"].items()]
    lines += ["", "## Most frequent avoidable tokens", "", "| Token | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in report["topAvoidableTokens"].items()]
    lines += ["", "## Most frequent spec identifiers", "", "| Identifier | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in report["topSpecIdentifiers"].items()]
    by_file = Counter(report["avoidableByFile"])
    lines += ["", "## Files with the most avoidable hits", "", "| File | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in by_file.most_common(30)]
    lines += ["", "## Every avoidable hit", "",
              "`path:line  category  token` (line 0 = the path itself; `[excepted]` = allowlisted).", "",
              "```text"]
    for hit in report["avoidable"]:
        suffix = "  [excepted]" if "exception" in hit else ""
        lines.append(f"{hit['path']}:{hit['line']}  {hit['category']}  {hit['token']}{suffix}")
    lines += ["```", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- baseline

def baseline_from(report: dict) -> dict:
    files = report["avoidableByFile"]
    return {"schema": BASELINE_SCHEMA, "repo": report["repo"], "sha": report["sha"],
            "total": sum(files.values()), "files": files}


def lint(report: dict, baseline: dict) -> list[str]:
    """Problems when any file carries more open avoidable hits than its baseline allows."""
    if baseline.get("schema") != BASELINE_SCHEMA or baseline.get("repo") != report["repo"]:
        return [f"baseline is not a {BASELINE_SCHEMA} document for {report['repo']}"]
    allowed = baseline["files"]
    problems = []
    for path, count in sorted(report["avoidableByFile"].items()):
        limit = allowed.get(path, 0)
        if count > limit:
            listing = [f"    {hit['path']}:{hit['line']}  {hit['category']}  {hit['token']}"
                       for hit in report["avoidable"] if hit["path"] == path and "exception" not in hit]
            problems.append(f"{path}: {count} avoidable vendor-term uses, baseline allows {limit}\n"
                            + "\n".join(listing))
    total = sum(report["avoidableByFile"].values())
    if total > baseline["total"]:
        problems.append(f"{report['repo']}: {total} avoidable vendor-term uses, baseline allows "
                        f"{baseline['total']}")
    return problems


def _mark_rename(old: str, new: str) -> bool:
    """``new`` is ``old`` with only marked path components renamed (``src/EsriLayer.cs`` -> ``src/Layer.cs``)."""
    old_parts, new_parts = old.split("/"), new.split("/")
    if len(old_parts) != len(new_parts) or old == new:
        return False
    return all(PRESCREEN.search(a) for a, b in zip(old_parts, new_parts) if a != b)


def baseline_growth(current: dict, base: dict | None) -> list[str]:
    """A baseline may only shrink: the total never grows, and no entry grows. The one exception is a rename
    that drops a mark: a new entry may take over the count of a removed entry whose path differs only in
    marked components, up to that entry's count (each removed entry covers one new entry)."""
    if base is None:
        return []
    name = current.get("repo", "?")
    problems = []
    if current["total"] > base["total"]:
        problems.append(f"baseline.{name}: total grew {base['total']} -> {current['total']}")
    removed = {path: count for path, count in base["files"].items() if path not in current["files"]}
    for path, count in sorted(current["files"].items()):
        before = base["files"].get(path, 0)
        if count <= before:
            continue
        source = next((old for old, old_count in sorted(removed.items())
                       if before == 0 and old_count >= count and _mark_rename(old, path)), None)
        if source is not None:
            del removed[source]
            continue
        problems.append(f"baseline.{name}: {path} grew {before} -> {count}; a baseline entry may only grow as "
                        "the rename of a removed entry whose marked path components were renamed")
    return problems


# --------------------------------------------------------------------------------------------- cli

def _report_for(args) -> dict:
    root = Path(args.root).resolve()
    vocabulary = load_vocabulary(Path(args.vocabulary))
    allowlist = load_allowlist(Path(args.allowlist) if args.allowlist else None)
    if args.git_ref:
        files = git_ref_files(root, args.git_ref)
        sha = _git(root, "rev-parse", args.git_ref).strip()
    else:
        files = working_tree_files(root)
        try:
            sha = _git(root, "rev-parse", "HEAD").strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            sha = "working-tree"
    hits, skipped, scanned = scan(files, args.repo, vocabulary, allowlist)
    return build_report(args.repo, args.sha or sha, hits, skipped, scanned, Path(args.vocabulary))


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="checkout to scan")
    parser.add_argument("--repo", required=True, help="repository name (baseline and allowlist key)")
    parser.add_argument("--git-ref", help="scan this commit's blobs instead of the working tree")
    parser.add_argument("--sha", help="override the recorded commit")
    parser.add_argument("--vocabulary", default=str(VOCABULARY))
    parser.add_argument("--allowlist", default=str(ALLOWLIST))


def _write(path: str | None, content: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    scan_parser = commands.add_parser("scan", help="write the per-repo report")
    _add_scan_arguments(scan_parser)
    scan_parser.add_argument("--json", dest="json_out")
    scan_parser.add_argument("--markdown", dest="markdown_out")

    lint_parser = commands.add_parser("lint", help="fail on avoidable uses above the baseline")
    _add_scan_arguments(lint_parser)
    lint_parser.add_argument("--baseline", help="default tools/vendor-terms/baseline.<repo>.json")
    lint_parser.add_argument("--json", dest="json_out")
    lint_parser.add_argument("--markdown", dest="markdown_out")

    baseline_parser = commands.add_parser("baseline", help="write the baseline from the current hits")
    _add_scan_arguments(baseline_parser)
    baseline_parser.add_argument("--out", help="default tools/vendor-terms/baseline.<repo>.json")

    shrink_parser = commands.add_parser("check-baselines", help="fail when a committed baseline grew")
    shrink_parser.add_argument("--base-ref", required=True)
    shrink_parser.add_argument("--repo-root", default=str(ROOT))

    args = parser.parse_args(argv)

    if args.command == "check-baselines":
        root = Path(args.repo_root)
        problems = []
        for path in sorted((root / "tools" / "vendor-terms").glob("baseline.*.json")):
            relative = path.relative_to(root).as_posix()
            try:
                base = json.loads(_git(root, "show", f"{args.base_ref}:{relative}"))
            except subprocess.CalledProcessError:
                base = None  # a new repository's first baseline
            problems += baseline_growth(json.loads(path.read_text(encoding="utf-8")), base)
        for problem in problems:
            print(f"::error::{problem}")
        print("vendor-term baselines: " + ("GREW" if problems else "only shrink — ok"))
        return 1 if problems else 0

    report = _report_for(args)
    default_baseline = TERMS_DIR / f"baseline.{args.repo}.json"

    if args.command == "baseline":
        out = Path(args.out) if args.out else default_baseline
        out.write_text(json.dumps(baseline_from(report), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out} ({sum(report['avoidableByFile'].values())} avoidable)")
        return 0

    _write(args.json_out, json.dumps(report, indent=2) + "\n")
    _write(args.markdown_out, render_markdown(report))
    counts = report["counts"]
    print(f"{report['repo']} @ {report['sha'][:12]}: spec={counts['spec']} nominative={counts['nominative']} "
          f"avoidable={counts['avoidable']} (excepted {counts['avoidableExcepted']})")
    if args.command == "scan":
        return 0

    baseline_path = Path(args.baseline) if args.baseline else default_baseline
    if not baseline_path.is_file():
        print(f"::error::no vendor-term baseline for {args.repo} at {baseline_path}; commit one with "
              "`check_vendor_terms.py baseline` in honua-release to adopt the gate")
        return 1
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    problems = lint(report, baseline)
    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print("New avoidable Esri/ArcGIS uses — see docs/THIRD-PARTY-TRADEMARKS.md: use a neutral name, "
              "make it a spec identifier or an attributed compatibility statement, or request an allowlist "
              "entry with a reason and an owner.")
        return 1
    shrinkable = {path: count for path, count in baseline["files"].items()
                  if report["avoidableByFile"].get(path, 0) < count}
    if shrinkable:
        print(f"{len(shrinkable)} baseline entries can shrink; regenerate the baseline to lock the burn-down in.")
    print(f"vendor-term lint: {sum(report['avoidableByFile'].values())} avoidable within baseline "
          f"{baseline['total']} — ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
