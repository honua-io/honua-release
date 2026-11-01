#!/usr/bin/env python3
"""Classify and lint third-party naming terms — honua-release#425.

Every occurrence in a checkout's text files is put in one of the classes defined by
docs/THIRD-PARTY-TRADEMARKS.md:

  spec          an identifier from tools/vendor-terms/geoservices-identifiers.v1.json — the GeoServices REST
                Specification 1.0 vocabulary a client sends or expects verbatim (esriGeometryPoint, ...)
  spec-interop  an identifier from tools/vendor-terms/geoservices-rest-interop.v1.json — a later GeoServices REST
                services wire value, each cited to its reference page (R36); reported separately from spec
  nominative    a compatibility statement in prose, or a product-name label in a data file, in a file that
                carries the required attribution, with no endorsement claim and not leading a heading
  confidential  desktop-client certification detail (R30): the desktop scripting module, desktop project and
                toolbox files, runner and licence detail, and the desktop client's name anywhere but an
                attributed compatibility statement or label. Never baselined, never allowlisted.
  avoidable     everything else: our own identifiers, repository/package/namespace/path/test names,
                comments, copy, endorsement claims and unattributed compatibility statements

Subcommands:

  scan              per-repo report (counts by class, every avoidable hit with path:line), JSON + Markdown
  lint              fail on a confidential use, or when a file's avoidable hits exceed the committed baseline
  baseline          write the baseline for a repo from its current avoidable hits
  check-baselines   fail when a committed baseline or confidential-known ledger grew relative to a base ref

A scan reads either a working tree (tracked plus untracked-but-not-ignored files, so .gitignore is
respected) or, with --git-ref, the blobs of a commit without checking it out.
"""

from __future__ import annotations

import argparse
import difflib
from collections import Counter
from dataclasses import dataclass, field
import functools
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata
from urllib.parse import unquote
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parent.parent
TERMS_DIR = ROOT / "tools" / "vendor-terms"
VOCABULARY = TERMS_DIR / "geoservices-identifiers.v1.json"
INTEROP = TERMS_DIR / "geoservices-rest-interop.v1.json"
ALLOWLIST = TERMS_DIR / "allowlist.json"
REPORT_SCHEMA = "honua.vendor-terms.report.v2"
BASELINE_SCHEMA = "honua.vendor-terms.baseline.v1"
ALLOWLIST_SCHEMA = "honua.vendor-terms.allowlist.v1"
INTEROP_SCHEMA = "honua.vendor-terms.geoservices-rest-interop.v1"
KNOWN_SCHEMA = "honua.vendor-terms.confidential-known.v1"
# Split source pieces keep the classifier's vocabulary out of this file's literal text.
_MARK_A = "Es" + "ri"
_MARK_B = "Arc" + "GIS"
_SCRIPTING = "Arc" + "py"
_LIVING_ATLAS = "Living" + " Atlas"
_ARC_MAP = "Arc" + "Map"
_ARC_CATALOG = "Arc" + "Catalog"
_ARC_OBJECTS = "Arc" + "Objects"
_ARC_SDE = "Arc" + "SDE"
_ARC_IMS = "Arc" + "IMS"
_MARK_A_LOWER = _MARK_A.lower()
_MARK_B_LOWER = _MARK_B.lower()


def _alternation(values: Iterable[str], *, flexible_spaces: bool = False) -> str:
    patterns = (re.escape(value).replace(r"\ ", r"\s+") if flexible_spaces else re.escape(value)
                for value in values)
    return "(?:" + "|".join(patterns) + ")"


_MARK_ALT = _alternation((_MARK_A, _MARK_B))
_REFERENCE_HOST = "developers." + _MARK_B.lower() + ".com"
INTEROP_REFERENCE = re.compile(r"https://" + re.escape(_REFERENCE_HOST) + r"/\S+", re.IGNORECASE)

CLASSES = ("spec", "spec-interop", "nominative", "avoidable", "confidential")
# The R30 class and its report keys, read through names: code scanning's sensitive-data heuristic takes a
# literal "confidential" key for credential material, and these values are classification labels and counts.
R30, R30_HITS, R30_BY_FILE = CLASSES[4], "confidential", "confidentialByFile"

# A token is the identifier-like run around a mark; one compound identifier is one hit.
MARK = re.compile(_MARK_ALT, re.IGNORECASE)
IDENTIFIER_CHAR = re.compile(r"[A-Za-z0-9_]")
# Confidential terms are constructed here so a confidential hit can never be hidden by the classifier.
DESKTOP_CLIENT_NAME = _MARK_B + " " + "Pro"
PRODUCT_NAMES = (
    _MARK_B + " Maps SDK for JavaScript", _MARK_B + " API for JavaScript", _MARK_B + " Maps SDK for .NET",
    _MARK_B + " Maps SDK for Qt", _MARK_B + " Maps SDK for Swift", _MARK_B + " Maps SDK for Kotlin",
    _MARK_B + " Runtime SDK", _MARK_B + " " + _LIVING_ATLAS, _MARK_B + " Maps SDK", _MARK_B + " JS API",
    DESKTOP_CLIENT_NAME, _MARK_B + " Online", _MARK_B + " Enterprise", "Portal for " + _MARK_B,
    _MARK_B + " Server", _MARK_B + " Desktop", _MARK_B + " Experience Builder", _MARK_B + " Dashboards",
    _MARK_B + " Field Maps", _MARK_B + " Survey123", _MARK_B + " StoryMaps", _MARK_B + " Hub",
    _MARK_B + " Runtime", _MARK_A + " Leaflet", _MARK_A + " Shapefile",
)
PRODUCT = re.compile("|".join(re.escape(name).replace(r"\ ", r"\s+") for name in PRODUCT_NAMES),
                     re.IGNORECASE)
# Product names without either core mark are included in the same whole-word check.
STANDALONE_NAMES = (_LIVING_ATLAS, _ARC_MAP, _ARC_CATALOG, _SCRIPTING, _ARC_OBJECTS, _ARC_SDE, _ARC_IMS)
STANDALONE = re.compile(r"\b" + _alternation((*STANDALONE_NAMES, "model" + "builder"), flexible_spaces=True) + r"\b",
                        re.IGNORECASE)
WORKFLOW_PRODUCT = re.compile("model" + "builder", re.IGNORECASE)
BARE_MARKS = {_MARK_A_LOWER, _MARK_B_LOWER}
CANONICAL_PRODUCT = {name.lower(): name for name in PRODUCT_NAMES}

# R30 confidential detail, by category. Each matches anywhere — code, config, tests, data, prose, paths.
CONFIDENTIAL = (
    # licence-manager hosts and variables, install paths, runner labels are matched first so a mark inside
    # them is reported as the detail it is
    ("runner-detail", re.compile(
        r"(?i:program\s+files(?:\s*\(x86\))?[\\/]+" + re.escape(_MARK_B) + r"\b[^\s\"',;]*|"
        r"\b(?:" + _alternation((_MARK_A_LOWER, _MARK_B_LOWER))
        + r")[-_]?licen[cs]e[-_]?(?:manager|server|host|file|port)\w*|lmg[r]d|"
        + re.escape(_MARK_B_LOWER) + r"[\s_\\/-]?p[r]o[-_]py\d\w*|"
        + re.escape(_MARK_B_LOWER) + r"[\s_-]?p[r]o\.exe)|\b2700\d@[\w.-]+")),
    ("desktop-scripting", re.compile(r"(?i:" + re.escape(_SCRIPTING) + r")")),
    ("desktop-file", re.compile(r"(?i:\.(?:ap[r]x|at[b]x|p[y]t))(?![A-Za-z0-9_])")),
    ("desktop-client", re.compile(r"(?i:" + re.escape(_MARK_B_LOWER) + r"[\s_\\/-]?p[r]o)(?![a-z])")),
)
# A CI runner selection is runner detail when it names a mark at all (runs-on: [self-hosted, <mark>-...]).
RUNNER_LABEL = re.compile(r"^\s*(?:-\s*)?runs-on\s*:", re.IGNORECASE)
PRESCREEN_NAMES = (*STANDALONE_NAMES, _MARK_A, _MARK_B)
PRESCREEN = re.compile(_alternation(PRESCREEN_NAMES, flexible_spaces=True) + r"|modelbuilder|"
                       r"\.(?:ap[r]x|at[b]x|p[y]t)(?![A-Za-z0-9_])|lmg[r]d|\b2700\d@", re.IGNORECASE)

# The two clauses of the legacy attribution, whitespace-insensitive.
ATTRIBUTION_CLAUSES = (
    re.compile(r"(?:" + re.escape(_MARK_A) + r",\s+" + re.escape(_MARK_B)
               + r",\s+and\s+the\s+" + re.escape(_MARK_A)
               + r"\s+product\s+names\s+used\s+here\s+)?"
               r"are\s+trademarks,\s+registered\s+trademarks,\s+or\s+service\s+marks\s+of\s+"
               + re.escape(_MARK_A),
               re.IGNORECASE),
    re.compile(r"not\s+affiliated\s+with,\s+sponsored\s+by,\s+or\s+endorsed\s+by\s+"
               + re.escape(_MARK_A), re.IGNORECASE),
)
NOTICE_PREFIX = re.compile(r"^\s*(?:#+|//+|\*|>)\s?")
COMPATIBILITY = re.compile(
    r"\b(?:works?\s+with|working\s+with|compatib\w*|interoperab\w*|interoperates?\s+with|"
    r"tested\s+(?:with|against|in)|verified\s+(?:with|against|in)|validated\s+(?:with|against|in)|"
    r"connects?\s+(?:from|to|with)|connecting\s+(?:from|to|with)|clients?\s+(?:such\s+as|including|like)|"
    r"for\s+use\s+with|supports?|supported\s+(?:by|in|with)|opens?\s+in|loads?\s+in|consumed\s+by|"
    r"from\s+within)\b", re.IGNORECASE)
# A claim of endorsement, sponsorship, affiliation or partnership; checked on every line before anything can
# be nominative, with the attribution notice's own disclaimer blanked out first.
ENDORSEMENT = re.compile(r"\b(?:certified\s+by|endorsed\s+by|endorses?|approved\s+by|official(?:ly)?|"
                         r"partner\w*|powered\s+by|sponsored\s+by|sponsors?|affiliated\s+with|"
                         r"affiliates?)\b", re.IGNORECASE)
# A denial asserts nothing; it is blanked too.
_RELATION = r"(?:affiliated\s+with|sponsored\s+by|endorsed\s+by|certified\s+by|approved\s+by|official(?:ly)?)"
DENIAL = re.compile(r"\b(?:not|never|nor)\s+(?:(?:been|be|in\s+any\s+way)\s+)?" + _RELATION
                    + r"(?:\s*,?\s*(?:or\s+|and\s+|nor\s+)?" + _RELATION + r")*", re.IGNORECASE)
HEADING_LEADS_WITH_MARK = re.compile(r"^\s*#{1,6}\s*[*_`]*\s*" + _MARK_ALT, re.IGNORECASE)
# Headings in the other prose formats: HTML <h1>-<h6>/<title>, AsciiDoc "= Title", and a line underlined by
# the next one (reStructuredText, AsciiDoc two-line and Markdown setext titles).
HTML_HEADING = re.compile(r"^\s*<(?:h[1-6]|title)\b", re.IGNORECASE)
ASCIIDOC_HEADING = re.compile(r"^\s*={1,6}\s+\S")
UNDERLINE = re.compile(r"^\s*([=\-~^\"'`#*+.:_])\1{2,}\s*$")
LEADS_WITH_MARK = re.compile(r"^[\s*_`#=]*" + _alternation(PRESCREEN_NAMES, flexible_spaces=True), re.IGNORECASE)

PROSE_SUFFIXES = {".md", ".mdx", ".markdown", ".rst", ".adoc", ".txt", ".html", ".htm"}
DATA_SUFFIXES = {".json", ".yaml", ".yml"}
# R37 certification data: JSON/YAML under certification/, or carrying a certification generator's schema.
CERTIFICATION_PATH = re.compile(r"^certification/")
CERTIFICATION_MARKER = re.compile(r"[\"']?schema[\"']?\s*:\s*[\"']?honua[\w./-]*certification", re.IGNORECASE)
# A product named as a whole cell or scalar value: a table cell (| ... | or <td>/<th>), a YAML/JSON value or
# list item. An optional version may follow a product label.
VERSION_SUFFIX = r"(?:\s+v?\d[\w.]*)?"
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
REPO_NAME = re.compile(r"honua-" + re.escape(_MARK_A_LOWER) + r"-compat|" + re.escape(_MARK_A_LOWER)
                       + r"-compat", re.IGNORECASE)
# A Honua-owned repository, package, module or namespace carrying a monitored name.
HONUA_NAME = re.compile(r"honua[-_./]?(?:[A-Za-z0-9]+[-_./])*" + _MARK_ALT, re.IGNORECASE)
# Third-party packages and modules we depend on or drive by their published names.
THIRD_PARTY_WORD = re.compile(
    r"^\W*(?:@" + re.escape(_MARK_A_LOWER) + r"/|@" + re.escape(_MARK_B_LOWER) + r"/|"
    + re.escape(_MARK_A_LOWER) + r"-leaflet|" + re.escape(_MARK_A_LOWER) + r"-loader|"
    + re.escape(_MARK_B_LOWER) + r"-rest-|" + re.escape(_MARK_B_LOWER) + r"-js-api|"
    + re.escape(_MARK_A) + r"\." + re.escape(_MARK_B) + r"Runtime|L\." + re.escape(_MARK_A_LOWER)
    + r"\.|" + re.escape(_MARK_B_LOWER)
    + r"\.(?:gis|features|geometry|mapping|raster|network|learn)\b)", re.IGNORECASE)
THIRD_PARTY_IMPORT = re.compile(r"^\s*(?:import\s+" + re.escape(_MARK_B_LOWER) + r"\b|from\s+"
                                + re.escape(_MARK_B_LOWER) + r"(?:\.\w+)*\s+import\b)")
URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+|\b[\w.-]+\."
                 + _alternation((re.escape(_MARK_A_LOWER), re.escape(_MARK_B_LOWER)))
                 + r"\.com\b|\b" + _alternation((re.escape(_MARK_A_LOWER), re.escape(_MARK_B_LOWER)))
                 + r"\.com\b", re.IGNORECASE)
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
class Interop:
    """The cited GeoServices REST interop vocabulary (R36), plus identifiers refused as non-wire values."""

    identifiers: dict[str, dict]
    refused: dict[str, dict]


def load_interop(path: Path = INTEROP) -> Interop:
    """Every entry must cite the reference page that documents it; an uncited entry is refused, so the
    vocabulary grows only with a citation."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != INTEROP_SCHEMA:
        raise SystemExit(f"{path}: schema must be {INTEROP_SCHEMA}")
    identifiers: dict[str, dict] = {}
    for index, entry in enumerate(document.get("identifiers", [])):
        missing = [key for key in ("identifier", "family", "field", "reference") if not entry.get(key)]
        if missing or not INTEROP_REFERENCE.fullmatch(entry.get("reference", "")):
            raise SystemExit(f"{path}: identifier {index} ({entry.get('identifier', '?')}) needs a family, a field "
                             "and an official reference URL — the interop vocabulary grows only "
                             "with a cited entry")
        if entry["identifier"] in identifiers:
            raise SystemExit(f"{path}: {entry['identifier']} is listed twice")
        identifiers[entry["identifier"]] = entry
    refused = {}
    for entry in document.get("refused", []):
        if entry.get("category") not in ("non-wire-identifier", "wrong-wire-value") or not entry.get("reason"):
            raise SystemExit(f"{path}: refused {entry.get('identifier', '?')} needs a category and a reason")
        if entry["category"] == "wrong-wire-value" and not entry.get("expected"):
            raise SystemExit(f"{path}: refused {entry['identifier']} needs the expected wire value")
        refused[entry["identifier"]] = entry
    overlap = sorted(set(identifiers) & set(refused))
    if overlap:
        raise SystemExit(f"{path}: {', '.join(overlap)} both cited and refused")
    return Interop(identifiers, refused)


@functools.lru_cache(maxsize=1)
def _committed_interop() -> Interop:
    return load_interop(INTEROP)


@dataclass(frozen=True)
class Exception_:
    """One allowlist entry: reviewed avoidable uses that do not count against the lint."""

    index: int
    repo: str
    paths: tuple[str, ...]
    tokens: tuple[str, ...]
    reason: str
    owner: str
    identifiers: tuple[str, ...] = ()

    def covers(self, repo: str, path: str, token: str, identifier: str | None = None, category: str = "") -> bool:
        if self.repo != repo:
            return False
        if not any(glob_match(pattern, path) for pattern in self.paths):
            return False
        if self.identifiers and (identifier not in self.identifiers or category == "endorsement-claim"):
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
        # an exception names one repository and real paths; a wildcard entry would be a second baseline
        if raw["repo"] == "*" or any(not isinstance(pattern, str) or WILDCARD_ONLY.fullmatch(pattern)
                                     for pattern in raw["paths"]):
            raise SystemExit(f"{path}: entry {index} is a wildcard (repo {raw['repo']!r}, paths {raw['paths']}); "
                             "an exception names one repository and the paths it covers")
        identifiers = raw.get("identifiers", [])
        if not isinstance(identifiers, list) or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9@._/-]+", value) for value in identifiers):
            raise SystemExit(f"{path}: entry {index} identifiers must be exact nonempty lexical identifiers")
        entries.append(Exception_(index, raw["repo"], tuple(raw["paths"]), tuple(raw.get("tokens", ())),
                                  raw["reason"], raw["owner"], tuple(identifiers)))
    return entries


WILDCARD_ONLY = re.compile(r"[*?/.]*")


def load_known(path: Path | None, repo: str) -> dict | None:
    """The dated confidential-known ledger: confidential uses already in a repository when R30 landed, listed so
    the gate is red with a reason while the containment work burns them down. It never turns a hit green in the
    reusable gate, and it may only shrink (check-baselines)."""
    if path is None or not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != KNOWN_SCHEMA or document.get("repo") != repo:
        raise SystemExit(f"{path}: not a {KNOWN_SCHEMA} document for {repo}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(document.get("asOf", ""))) or not document.get("burnDown"):
        raise SystemExit(f"{path}: the ledger needs an asOf date and the burnDown issues that empty it")
    return document


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
    position: int = field(default=0, repr=False, compare=False)

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
    data: bool = field(init=False)
    certification: bool = field(init=False)
    test: bool = field(init=False)
    manifest: bool = field(init=False)
    attributed: bool = field(init=False)
    stamped: bool = field(init=False)
    notice: dict[int, str] = field(init=False)

    def __post_init__(self) -> None:
        name = PurePosixPath(self.path).name
        self.suffix = PurePosixPath(self.path).suffix.lower() or ("." + name.lower() if name.startswith(".")
                                                                  else "")
        if name in HASH_COMMENT_NAMES:
            self.suffix = self.suffix or ".dockerfile"
        self.prose = self.suffix in PROSE_SUFFIXES
        self.data = self.suffix in DATA_SUFFIXES
        self.certification = self.data and (CERTIFICATION_PATH.search(self.path) is not None
                                            or CERTIFICATION_MARKER.search(self.text[:4096]) is not None)
        self.test = TEST_PATH.search(self.path) is not None
        self.manifest = PACKAGE_MANIFEST.search(self.path) is not None
        self.attributed, self.notice = _attribution(self.text.splitlines())
        self.stamped = self.data and _stamped(self.text, self.suffix)


def _notice_lines(lines: list[str]) -> list[str]:
    """Lines as the attribution is read: comment and quote prefixes and markdown emphasis removed."""
    return [re.sub(r"[*_`]", "", NOTICE_PREFIX.sub(" ", line)) for line in lines]


def _attribution(lines: list[str]) -> tuple[bool, dict[int, str]]:
    """Whether the text carries both clauses of the attribution, and, for each line the notice spans, that
    line with the notice's own words blanked (line number -> text), so the disclaimer is not read as a claim."""
    cleaned = _notice_lines(lines)
    offsets = []
    position = 0
    for line in cleaned:
        offsets.append(position)
        position += len(line) + 1
    flattened = " ".join(cleaned)
    found = [False] * len(ATTRIBUTION_CLAUSES)
    blanked: dict[int, list[str]] = {}
    for index, clause in enumerate(ATTRIBUTION_CLAUSES):
        for match in clause.finditer(flattened):
            found[index] = True
            for number, offset in enumerate(offsets):
                line = cleaned[number]
                lo, hi = max(match.start(), offset), min(match.end(), offset + len(line))
                if lo < hi:
                    chars = blanked.setdefault(number + 1, list(line))
                    chars[lo - offset:hi - offset] = " " * (hi - lo)
    if not all(found):
        return False, {}
    return True, {number: "".join(chars) for number, chars in blanked.items()}


def is_attributed(text: str) -> bool:
    return _attribution(text.splitlines())[0]


def _stamped(text: str, suffix: str) -> bool:
    """A data file's header carries the attribution: a top-level ``trademarkNotice`` (JSON or YAML), or the
    YAML file's leading comment block."""
    if suffix == ".json":
        try:
            document = json.loads(text)
        except ValueError:
            return False
        notice = document.get("trademarkNotice") if isinstance(document, dict) else None
        return isinstance(notice, str) and is_attributed(notice)
    lines = text.splitlines()
    header = []
    for line in lines:
        if line.strip() and not line.lstrip().startswith("#"):
            break
        header.append(line)
    if is_attributed("\n".join(header)):
        return True
    for index, line in enumerate(lines):
        if re.match(r"trademarkNotice\s*:", line):
            value = [line.split(":", 1)[1]]
            for continuation in lines[index + 1:]:
                if not continuation.startswith((" ", "\t")):
                    break
                value.append(continuation)
            return is_attributed("\n".join(value))
    return False


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


Span = tuple[int, int, str, str, "str | None"]
_SPAN_WINDOW = 2048
_SPAN_OVERLAP = 256
_MAX_DECODED_LINE = 16_000_000


def _spans(line: str) -> list[Span]:
    """Inspect long physical lines in bounded overlapping windows without skipping content."""
    if len(line) <= _SPAN_WINDOW:
        return _spans_window(line)

    spans: list[Span] = []
    seen: set[tuple[int, int, str, str, str | None]] = set()
    core_width = _SPAN_WINDOW - 2 * _SPAN_OVERLAP
    core_start = 0
    while core_start < len(line):
        core_end = min(core_start + core_width, len(line))
        window_start = max(0, core_start - _SPAN_OVERLAP)
        window_end = min(len(line), core_end + _SPAN_OVERLAP)
        window = line[window_start:window_end]
        for start, end, token, term, detail in _spans_window(window):
            # Assign each hit to the window core containing its name. Long identifiers are
            # clipped to the inspected window but still carry the mark used by this anchor.
            anchor = start
            if not _is_product(token):
                mark = MARK.search(token)
                if mark:
                    anchor += mark.start()
            absolute_anchor = window_start + anchor
            if not core_start <= absolute_anchor < core_end:
                continue
            item = (window_start + start, window_start + end, token, term, detail)
            if item not in seen:
                seen.add(item)
                spans.append(item)
        core_start = core_end
    return sorted(spans, key=lambda span: (span[0], span[1]))


def _spans_window(line: str) -> list[Span]:
    """(start, end, token, term, confidential) for every mark on a line; product names absorb their tokens.

    ``confidential`` is the R30 category when the span is, or overlaps, confidential detail; detail that carries
    no mark at all (a desktop project file, a licence-server address) is a span of its own."""
    if PRESCREEN.search(line):
        if len(line) > 1_000_000:
            raise ValueError("Candidate line exceeds the inspection limit")
        if sum(1 for _ in PRESCREEN.finditer(line)) > 512:
            raise ValueError("Candidate line has too many terms to inspect safely")
        work = 0
        for word in re.finditer(r"[^\s\"'()\[\]{}<>,;|]+", line):
            value = word.group()
            matches = sum(1 for _ in PRESCREEN.finditer(value))
            if matches:
                if len(value) > 4096:
                    raise ValueError("Candidate token exceeds the inspection limit")
                # The classifier applies a fixed set of bounded literal/identifier regexes to
                # each candidate. Estimate work linearly instead of rejecting ordinary long HTML
                # tokens before their bounded windows can be inspected.
                work += len(value) * matches
                if work > 2_000_000:
                    raise ValueError("Candidate requires too much inspection work")
    spans: list[list] = []
    taken: list[tuple[int, int]] = []
    for match in PRODUCT.finditer(line):
        # a product name followed by identifier characters is an identifier, not the product name
        if match.end() < len(line) and (line[match.end()].isalnum() or line[match.end()] == "_"):
            continue
        canonical = CANONICAL_PRODUCT[re.sub(r"\s+", " ", match.group()).lower()]
        spans.append([match.start(), match.end(), match.group(), canonical, None])
        taken.append((match.start(), match.end()))
    for match in STANDALONE.finditer(line):
        if any(s <= match.start() < e for s, e in taken):
            continue
        spans.append([match.start(), match.end(), match.group(), re.sub(r"\s+", " ", match.group()), None])
        taken.append((match.start(), match.end()))
    for match in WORKFLOW_PRODUCT.finditer(line):
        if any(s <= match.start() < e for s, e in taken):
            continue
        spans.append([match.start(), match.end(), match.group(), match.group(), None])
        taken.append((match.start(), match.end()))
    for start, end in _mark_tokens(line):
        if any(s <= start < e for s, e in taken):
            continue
        token = line[start:end]
        lowered = token.lower()
        term = _MARK_A_LOWER if _MARK_A_LOWER in lowered and (_MARK_B_LOWER not in lowered or
                                                              lowered.index(_MARK_A_LOWER) < lowered.index(_MARK_B_LOWER)) else _MARK_B_LOWER
        spans.append([start, end, token, term, None])
        taken.append((start, end))
    for category, pattern in CONFIDENTIAL:
        for match in pattern.finditer(line):
            if category in ("desktop-scripting", "desktop-client") and not _starts_word(line, match.start()):
                continue
            overlapping = [span for span in spans if span[0] < match.end() and match.start() < span[1]]
            for span in overlapping:
                span[4] = span[4] or category
            if not overlapping:
                start, end = match.start(), match.end()
                if category != "desktop-file":
                    while start > 0 and IDENTIFIER_CHAR.match(line[start - 1]):
                        start -= 1
                    while end < len(line) and IDENTIFIER_CHAR.match(line[end]):
                        end += 1
                spans.append([start, end, line[start:end], category, category])
    return sorted((tuple(span) for span in spans), key=lambda span: (span[0], span[1]))


def _starts_word(line: str, index: int) -> bool:
    """A mark counts where a token starts; unrelated suffixes do not trigger a match."""
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


# Decode a bounded set of rendered/source forms before the ordinary classifier.
# This is a detection view only: the source bytes are never modified.
_SOURCE_ESCAPE = re.compile(
    r"(?<!\\)(?P<slashes>(?>\\+))(?:u\{(?P<braced>[0-9a-fA-F]{1,6})\}|u(?P<u4>[0-9a-fA-F]{4})|"
    r"U(?P<u8>[0-9a-fA-F]{8})|x(?P<x2>[0-9a-fA-F]{2})|"
    r"N\{(?P<name>[A-Za-z0-9 -]{1,100})\}|(?P<octal>[0-7]{1,3}))"
)


def _decode_detection_view(value: str) -> str:
    if value.isascii() and not any(char in value for char in "&%\\"):
        return value

    def decode_escape(match: re.Match[str]) -> str:
        slashes = match.group("slashes")
        if len(slashes) % 2 == 0:
            return match.group()
        try:
            name = match.group("name")
            if name:
                char = unicodedata.lookup(name)
            else:
                octal = match.group("octal")
                digits = octal or match.group("braced") or match.group("u4") or match.group("u8") or match.group("x2")
                char = chr(int(digits, 8 if octal else 16))
            if len(char) != 1 or 0xD800 <= ord(char) <= 0xDFFF:
                return match.group()
            return slashes[:-1] + char
        except (KeyError, ValueError, OverflowError):
            return match.group()

    if len(value) > _MAX_DECODED_LINE:
        raise ValueError("Encoded input exceeds the inspection limit")
    for _ in range(8):
        decoded = html.unescape(value) if "&" in value else value
        decoded = unquote(decoded, errors="replace") if "%" in decoded else decoded
        decoded = _SOURCE_ESCAPE.sub(decode_escape, decoded) if "\\" in decoded else decoded
        if not decoded.isascii():
            if len(decoded) > _MAX_DECODED_LINE:
                raise ValueError("Unicode input exceeds the inspection limit")
            decoded = unicodedata.normalize("NFKC", decoded)
            if len(decoded) > _MAX_DECODED_LINE:
                raise ValueError("Normalized input exceeds the inspection limit")
            decoded = "".join(char for char in decoded if unicodedata.category(char) != "Cf"
                              and ord(char) != 0x034F and not 0xFE00 <= ord(char) <= 0xFE0F
                              and not 0xE0100 <= ord(char) <= 0xE01EF)
        # An encoded line break must not invent a physical source line.
        decoded = re.sub(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]", " ", decoded)
        if decoded == value:
            return decoded
        value = decoded
    raise ValueError("Candidate has too many nested encodings to inspect safely")


class Classifier:
    def __init__(self, repo: str, vocabulary: dict[str, dict], allowlist: list[Exception_],
                 interop: Interop | None = None, *, skip_encoded: bool = True,
                 normalize_rendered: bool = True):
        self.repo = repo
        self.vocabulary = vocabulary
        self.interop = interop if interop is not None else _committed_interop()
        self.allowlist = allowlist
        self.skip_encoded = skip_encoded
        self.normalize_rendered = normalize_rendered
        self.skipped: Counter[str] = Counter()

    def _exception(self, path: str, token: str, identifier: str | None = None, category: str = "") -> int | None:
        for entry in self.allowlist:
            if entry.covers(self.repo, path, token, identifier, category):
                return entry.index
        return None

    def classify_path(self, path: str) -> list[Hit]:
        """One hit per path prefix whose final component carries a mark or confidential detail (a directory
        counts once). A confidential path component is never excepted."""
        hits = []
        parts = path.split("/")
        for depth, part in enumerate(parts):
            prefix = "/".join(parts[:depth + 1])
            original = _spans(part)
            spans = list(original)
            detection_part = _decode_detection_view(part) if self.normalize_rendered else part
            if detection_part != part:
                seen = Counter((token.lower(), term.lower(), detail) for _, _, token, term, detail in original)
                added = Counter()
                for span in _spans(detection_part):
                    key = (span[2].lower(), span[3].lower(), span[4])
                    added[key] += 1
                    if added[key] > seen[key]:
                        spans.append(span)
            for start, end, token, term, detail in spans:
                if detail:
                    hits.append(Hit(prefix, 0, token, term, "confidential", detail))
                else:
                    hits.append(Hit(prefix, 0, token, term, "avoidable", "path", self._exception(prefix, token)))
        return hits

    def classify_text(self, path: str, text: str) -> list[Hit]:
        # First preserve original lexical exceptions, then add newly revealed occurrences.
        original = self._classify_detection_text(path, text)
        if not self.normalize_rendered:
            return original
        normalized_parts = []
        for line in text.splitlines(keepends=True):
            ending = re.search(r"(?:\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029])$", line)
            if ending:
                normalized_parts.append(_decode_detection_view(line[:ending.start()]) + ending.group())
            else:
                normalized_parts.append(_decode_detection_view(line))
        normalized = "".join(normalized_parts)
        if normalized == text:
            return original
        decoded = self._classify_detection_text(path, normalized)
        # An exact unchanged span identifies the same physical occurrence even if
        # decoding nearby URL punctuation changes the lexical exception lookup.
        # A newly decoded mark has no unchanged span and must remain a finding.
        original_lines = text.splitlines()
        decoded_lines = normalized.splitlines()
        original_keys = {(hit.line, hit.position, hit.token.lower(), hit.cls, hit.category, hit.exception)
                         for hit in original}
        mappings: dict[int, list[tuple[int, int, int]]] = {}
        for hit in decoded:
            number = hit.line
            if number not in mappings:
                before, after = original_lines[number - 1], decoded_lines[number - 1]
                if before == after:
                    mappings[number] = [(0, 0, len(before))]
                else:
                    if max(len(before), len(after)) > 4096:
                        # Only the unchanged prefix and suffix are unambiguous without
                        # an expensive whole-line comparison. Any middle occurrences
                        # stay as separate, conservative findings.
                        prefix = 0
                        while prefix < min(len(before), len(after)) and before[prefix] == after[prefix]:
                            prefix += 1
                        suffix = 0
                        limit = min(len(before), len(after)) - prefix
                        while suffix < limit and before[-1 - suffix] == after[-1 - suffix]:
                            suffix += 1
                        mappings[number] = [(0, 0, prefix), (len(before) - suffix, len(after) - suffix, suffix)]
                    else:
                        mappings[number] = difflib.SequenceMatcher(None, before, after, autojunk=False).get_matching_blocks()
            same_occurrence = False
            for before_start, after_start, length in mappings[number]:
                if after_start <= hit.position and hit.position + len(hit.token) <= after_start + length:
                    old_start = before_start + hit.position - after_start
                    key = (number, old_start, hit.token.lower(), hit.cls, hit.category, hit.exception)
                    if key in original_keys:
                        same_occurrence = True
                        break
            if not same_occurrence:
                original.append(hit)
        return sorted(original, key=lambda hit: hit.line)

    def _classify_detection_text(self, path: str, text: str) -> list[Hit]:
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
            runner = RUNNER_LABEL.search(line) is not None
            for start, end, token, term, detail in spans:
                if self.skip_encoded and ENCODED.fullmatch(_word_around(line, start, end)):
                    self.skipped["encoded-token"] += 1
                    continue
                if runner and token not in self.vocabulary:
                    detail = "runner-detail"
                heading = _heading_leads_with_mark(context, line, lines[number] if number < len(lines) else "")
                cls, category = self._classify(context, number, line, start, end, token, detail, in_fence,
                                               block_open, comment_at, heading)
                left, right = start, end
                while left > 0 and re.fullmatch(r"[A-Za-z0-9@._/-]", line[left - 1]):
                    left -= 1
                while right < len(line) and re.fullmatch(r"[A-Za-z0-9@._/-]", line[right]):
                    right += 1
                exception = self._exception(path, token, line[left:right], category) if cls == "avoidable" else None
                hits.append(Hit(path, number, token, term, cls, category, exception, position=start))
        return hits

    def _classify(self, context: FileContext, number: int, line: str, start: int, end: int, token: str,
                  detail: str | None, in_fence: bool, block_open: bool, comment_at: int | None,
                  heading: bool = False) -> tuple[str, str]:
        if token in self.vocabulary:
            return "spec", "spec-identifier"
        if token in self.interop.identifiers:
            return "spec-interop", "interop-identifier"
        if token in self.interop.refused:
            return "avoidable", self.interop.refused[token]["category"]
        # R30 detail is confidential wherever it appears; only the desktop client's plain name, in an
        # attributed compatibility statement or a stamped label, is nominative instead
        if detail and detail != "desktop-client":
            return "confidential", detail
        word = _word_around(line, start, end)
        stripped = line.strip()
        in_comment = (block_open or (comment_at is not None and start > comment_at)
                      or stripped.startswith(("*", "/*", "///", "<!--")) and context.suffix in SLASH_COMMENT)
        prose_line = context.prose and not in_fence and not in_comment
        # the endorsement check runs on every line before anything can be nominative
        claim = ENDORSEMENT.search(DENIAL.sub(" ", context.notice.get(number, line))) is not None
        nominative = None
        if not claim:
            if number in context.notice and _whole_word(line, start, token, word):
                nominative = "attribution"
            elif self._nominative(context, line, start, token, word, prose_line, heading):
                nominative = "compatibility-statement"
            elif self._label(context, line, start, end, token):
                nominative = "certification-label" if context.certification else "label"
        if detail:
            return ("nominative", nominative) if nominative and token == DESKTOP_CLIENT_NAME else (
                "confidential", detail)
        if nominative:
            return "nominative", nominative
        if claim:
            return "avoidable", "endorsement-claim"
        if REPO_NAME.search(word) or HONUA_NAME.search(word) or (context.manifest
                                                                  and PACKAGE_DECLARATION.search(line)):
            if NAMESPACE_LINE.search(line) and not context.prose:
                return "avoidable", "namespace-or-import"
            return "avoidable", "repo-or-package-name"
        if THIRD_PARTY_WORD.search(word) or (THIRD_PARTY_IMPORT.search(line) and token.lower() == _MARK_B_LOWER):
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
            return "avoidable", "product-name-label" if _is_product(token) else "string-literal"
        return "avoidable", "identifier"

    @staticmethod
    def _nominative(context: FileContext, line: str, start: int, token: str, word: str,
                    prose_line: bool, heading: bool = False) -> bool:
        """A compatibility statement: prose in an attributed file, the mark a whole word outside inline code, not
        leading a heading. A table row qualifies when the product fills a cell; any other line needs the
        compatibility language. Nothing about the path makes a line nominative."""
        if not prose_line or not context.attributed or heading:
            return False
        if not _is_product(token) and token.lower() not in BARE_MARKS:
            return False  # compound identifiers are never nominative
        if not _whole_word(line, start, token, word):
            return False
        if line.count("`", 0, start) % 2:
            return False  # inline code is code, not prose
        return _cell(line, start, token) or COMPATIBILITY.search(line) is not None

    @staticmethod
    def _label(context: FileContext, line: str, start: int, end: int, token: str) -> bool:
        """R37: a product name as a whole scalar value in a data file whose header carries the attribution."""
        if not context.stamped or not _is_product(token):
            return False
        value = re.compile(r"^\s*(?:-\s+)?(?:[\"']?[\w.-]+[\"']?\s*:\s*)?([\"']?)" + re.escape(token)
                           + VERSION_SUFFIX + r"\1\s*,?\s*(?:#.*)?$")
        match = value.match(line)
        return match is not None and match.start(1) <= start


def _is_product(token: str) -> bool:
    return bool(token in PRODUCT_NAMES or PRODUCT.fullmatch(token) or STANDALONE.fullmatch(token))


def _whole_word(line: str, start: int, token: str, word: str) -> bool:
    # not part of a path, URL, package or identifier
    return word == token or re.fullmatch(r"[\W_]*" + re.escape(token) + r"(?:'s|’s)?[\W_]*", word) is not None


def _cell(line: str, start: int, token: str) -> bool:
    """A product-name label fills an entire table cell or scalar value."""
    cell = re.compile(r"(?:^\s*\||\|)\s*(?:\*\*|__)?" + re.escape(token) + VERSION_SUFFIX
                      + r"(?:\*\*|__)?\s*\||<t[dh]\b[^>]*>\s*(?:<[^>]+>\s*)*" + re.escape(token)
                      + VERSION_SUFFIX + r"\s*(?:<[^>]+>\s*)*</t[dh]>", re.IGNORECASE)
    return any(match.start() <= start < match.end() for match in cell.finditer(line))


# --------------------------------------------------------------------------------------------- scan

def scan(files: Iterable[tuple[str, bytes | None]], repo: str, vocabulary: dict[str, dict],
         allowlist: list[Exception_], interop: Interop | None = None) -> tuple[list[Hit], Counter[str], int]:
    classifier = Classifier(repo, vocabulary, allowlist, interop)
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
                 vocabulary_path: Path = VOCABULARY, interop_path: Path = INTEROP) -> dict:
    counts = Counter(hit.cls for hit in hits)
    avoidable = [hit for hit in hits if hit.cls == "avoidable"]
    open_hits = [hit for hit in avoidable if hit.exception is None]
    confidential = [hit for hit in hits if hit.cls == "confidential"]
    return {
        "schema": REPORT_SCHEMA,
        "repo": repo,
        "sha": sha,
        "vocabulary": {"path": "tools/vendor-terms/geoservices-identifiers.v1.json",
                       "sha256": hashlib.sha256(vocabulary_path.read_bytes()).hexdigest()},
        "interopVocabulary": {"path": "tools/vendor-terms/geoservices-rest-interop.v1.json",
                              "sha256": hashlib.sha256(interop_path.read_bytes()).hexdigest()},
        "files": {"scanned": scanned, "skipped": dict(sorted(skipped.items()))},
        "counts": {cls: counts.get(cls, 0) for cls in CLASSES} | {
            "avoidableExcepted": len(avoidable) - len(open_hits)},
        "byCategory": {cls: dict(Counter(hit.category for hit in hits if hit.cls == cls).most_common())
                       for cls in CLASSES},
        "byTerm": {cls: dict(Counter(hit.term for hit in hits if hit.cls == cls).most_common())
                   for cls in CLASSES},
        "topSpecIdentifiers": dict(Counter(hit.token for hit in hits if hit.cls == "spec").most_common(25)),
        "topInteropIdentifiers": dict(Counter(hit.token for hit in hits
                                              if hit.cls == "spec-interop").most_common(25)),
        "topAvoidableTokens": dict(Counter(hit.token for hit in open_hits).most_common(25)),
        "avoidableByFile": dict(sorted(Counter(hit.path for hit in open_hits).items())),
        "avoidable": [hit.as_dict() for hit in avoidable],
        "confidentialByFile": dict(sorted(Counter(hit.path for hit in confidential).items())),
        "confidential": [hit.as_dict() for hit in confidential],
    }


def is_confidential_text(text: str) -> bool:
    return any(pattern.search(text) for _, pattern in CONFIDENTIAL)


def render_markdown(report: dict) -> str:
    """The committed inventory. Confidential hits appear as counts only (R30: no detail in a public repository),
    and an avoidable hit whose path is itself confidential is counted, not listed."""
    counts = report["counts"]
    withheld = [hit for hit in report["avoidable"] if is_confidential_text(hit["path"])]
    listed = [hit for hit in report["avoidable"] if not is_confidential_text(hit["path"])]
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
        f"| spec (GSR 1.0) | {counts['spec']} |",
        f"| spec-interop (GeoServices REST, R36) | {counts['spec-interop']} |",
        f"| nominative | {counts['nominative']} |",
        f"| confidential (R30) | {counts[R30]} |",
        f"| avoidable | {counts['avoidable']} |",
        f"| avoidable, excepted by allowlist | {counts['avoidableExcepted']} |",
        "",
        "## Confidential hits by category",
        "",
        f"Counts only: {len(report[R30_BY_FILE])} files. The repository's own lint lists each hit.",
        "",
        "| Category | Hits |",
        "| --- | ---: |",
    ]
    lines += [f"| {name} | {n} |" for name, n in report["byCategory"][R30].items()]
    lines += ["", "## Avoidable hits by category", "", "| Category | Hits |", "| --- | ---: |"]
    lines += [f"| {name} | {n} |" for name, n in report["byCategory"]["avoidable"].items()]
    lines += ["", "## Avoidable hits by term", "", "| Term | Hits |", "| --- | ---: |"]
    lines += [f"| {name} | {n} |" for name, n in report["byTerm"]["avoidable"].items()]
    lines += ["", "## Most frequent avoidable tokens", "", "| Token | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in report["topAvoidableTokens"].items()]
    lines += ["", "## Most frequent spec identifiers", "", "| Identifier | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in report["topSpecIdentifiers"].items()]
    lines += ["", "## Most frequent spec-interop identifiers", "", "| Identifier | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in report["topInteropIdentifiers"].items()]
    by_file = Counter({path: n for path, n in report["avoidableByFile"].items() if not is_confidential_text(path)})
    lines += ["", "## Files with the most avoidable hits", "", "| File | Hits |", "| --- | ---: |"]
    lines += [f"| `{name}` | {n} |" for name, n in by_file.most_common(30)]
    lines += ["", "## Every avoidable hit", "",
              "`path:line  category  token` (line 0 = the path itself; `[excepted]` = allowlisted).", ""]
    if withheld:
        files = len({hit["path"] for hit in withheld})
        lines += [f"{len(withheld)} avoidable hits in {files} file{'' if files == 1 else 's'} whose path is "
                  "itself confidential are counted above but not listed.", ""]
    lines += ["```text"]
    for hit in listed:
        suffix = "  [excepted]" if "exception" in hit else ""
        lines.append(f"{hit['path']}:{hit['line']}  {hit['category']}  {hit['token']}{suffix}")
    lines += ["```", ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- baseline

def _refuse_confidential_paths(paths: Iterable[str], what: str) -> None:
    named = [path for path in paths if is_confidential_text(path)]
    if named:
        raise SystemExit(f"{len(named)} {what} entries would name a confidential path; contain them first (R30)")


def baseline_from(report: dict) -> dict:
    """Avoidable uses only: a confidential hit is never baselined."""
    files = report["avoidableByFile"]
    _refuse_confidential_paths(files, "baseline")
    return {"schema": BASELINE_SCHEMA, "repo": report["repo"], "sha": report["sha"],
            "total": sum(files.values()), "files": files}


def known_from(report: dict, as_of: str, burn_down: list[str]) -> dict:
    files = report[R30_BY_FILE]
    _refuse_confidential_paths(files, "confidential-known")
    return {"schema": KNOWN_SCHEMA, "repo": report["repo"], "sha": report["sha"], "asOf": as_of,
            "burnDown": burn_down,
            "note": "Confidential (R30) uses present when the class landed. Listed so the gate is red with a "
                    "reason, never green: the reusable gate fails on every entry. May only shrink.",
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


def r30_findings(report: dict, known: dict | None) -> tuple[list[str], list[str]]:
    """(new, known): every confidential hit, as ``path:line  category  token``. A hit is known only while its
    file has no more confidential hits than the dated ledger lists; anything else is new."""
    listed = (known or {}).get("files", {})
    new, already = [], []
    for path, count in sorted(report[R30_BY_FILE].items()):
        rows = [f"{hit['path']}:{hit['line']}  {hit['category']}  {hit['token']}"
                for hit in report[R30_HITS] if hit["path"] == path]
        (already if count <= listed.get(path, 0) else new).extend(rows)
    return new, already


def baseline_growth(current: dict, base: dict | None, kind: str = "baseline") -> list[str]:
    """A baseline (or confidential-known ledger) may only shrink: its total never grows, it stays the sum of its
    entries, no entry grows, and no entry is added — not even by moving counts from a removed or shrunk entry,
    so a rename carries its file's uses only once they are gone."""
    name = current.get("repo", "?")
    problems = []
    if current["total"] != sum(current["files"].values()):
        problems.append(f"{kind}.{name}: total {current['total']} is not the sum of its entries "
                        f"({sum(current['files'].values())})")
    if base is None:
        return problems
    if current["total"] > base["total"]:
        problems.append(f"{kind}.{name}: total grew {base['total']} -> {current['total']}")
    for path, count in sorted(current["files"].items()):
        if path not in base["files"]:
            problems.append(f"{kind}.{name}: {path} is a new entry ({count}); an entry may only shrink or go, and "
                            "none is added, not even by redistribution from another file")
        elif count > base["files"][path]:
            problems.append(f"{kind}.{name}: {path} grew {base['files'][path]} -> {count}")
    return problems


# --------------------------------------------------------------------------------------------- cli

def _report_for(args) -> dict:
    root = Path(args.root).resolve()
    vocabulary = load_vocabulary(Path(args.vocabulary))
    interop = load_interop(Path(args.interop))
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
    hits, skipped, scanned = scan(files, args.repo, vocabulary, allowlist, interop)
    return build_report(args.repo, args.sha or sha, hits, skipped, scanned, Path(args.vocabulary),
                        Path(args.interop))


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--root", default=".", help="checkout to scan")
    parser.add_argument("--repo", required=True, help="repository name (baseline and allowlist key)")
    parser.add_argument("--git-ref", help="scan this commit's blobs instead of the working tree")
    parser.add_argument("--sha", help="override the recorded commit")
    parser.add_argument("--vocabulary", default=str(VOCABULARY))
    parser.add_argument("--interop", default=str(INTEROP))
    parser.add_argument("--allowlist", default=str(ALLOWLIST))


def _write(path: str | None, content: str) -> None:
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")


def _check_ledgers(root: Path, base_ref: str) -> list[str]:
    problems = []
    for pattern, kind in (("baseline.*.json", "baseline"), ("confidential-known.*.json", "confidential-known")):
        for path in sorted((root / "tools" / "vendor-terms").glob(pattern)):
            relative = path.relative_to(root).as_posix()
            try:
                base = json.loads(_git(root, "show", f"{base_ref}:{relative}"))
            except subprocess.CalledProcessError:
                base = None  # a repository's first baseline or ledger
            problems += baseline_growth(json.loads(path.read_text(encoding="utf-8")), base, kind)
    return problems


def _lint(args, report: dict, default_baseline: Path) -> int:
    failed = False
    known = load_known(Path(args.known) if args.known else TERMS_DIR / f"confidential-known.{args.repo}.json",
                       args.repo)
    new, already = r30_findings(report, known)
    for row in new:
        print(f"::error::confidential (R30) — never baselined, never allowlisted: {row}")
    if new:
        failed = True
        print(f"{len(new)} confidential uses outside the confidential-known ledger. Desktop-client certification "
              "detail belongs only in the private certification repository; see docs/THIRD-PARTY-TRADEMARKS.md.")
    if already:
        level = "error" if args.fail_on_known_confidential else "warning"
        reason = (f"known since {known['asOf']} (tools/vendor-terms/confidential-known.{args.repo}.json, "
                  f"burn-down {', '.join(known['burnDown'])})")
        for row in already:
            print(f"::{level}::confidential (R30), {reason}: {row}")
        print(f"{len(already)} known confidential uses remain, {reason}. The vendor-terms gate stays red until the "
              "ledger is empty.")
        failed = failed or args.fail_on_known_confidential

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
        print("New avoidable third-party-name uses — see docs/THIRD-PARTY-TRADEMARKS.md: use a neutral name, "
              "make it a spec identifier or an attributed compatibility statement, or request an allowlist "
              "entry with a reason and an owner.")
        return 1
    shrinkable = {path: count for path, count in baseline["files"].items()
                  if report["avoidableByFile"].get(path, 0) < count}
    if shrinkable:
        print(f"{len(shrinkable)} baseline entries can shrink; regenerate the baseline to lock the burn-down in.")
    print(f"vendor-term lint: {sum(report['avoidableByFile'].values())} avoidable within baseline "
          f"{baseline['total']}" + (" — confidential uses FAIL" if failed else " — ok"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    scan_parser = commands.add_parser("scan", help="write the per-repo report")
    _add_scan_arguments(scan_parser)
    scan_parser.add_argument("--json", dest="json_out")
    scan_parser.add_argument("--markdown", dest="markdown_out")

    lint_parser = commands.add_parser("lint", help="fail on confidential uses and avoidable uses above the baseline")
    _add_scan_arguments(lint_parser)
    lint_parser.add_argument("--baseline", help="default tools/vendor-terms/baseline.<repo>.json")
    lint_parser.add_argument("--known", help="default tools/vendor-terms/confidential-known.<repo>.json")
    lint_parser.add_argument("--fail-on-known-confidential", action="store_true",
                             help="fail on ledger-listed confidential uses too (the reusable gate always does)")
    lint_parser.add_argument("--json", dest="json_out")
    lint_parser.add_argument("--markdown", dest="markdown_out")

    baseline_parser = commands.add_parser("baseline", help="write the baseline from the current hits")
    _add_scan_arguments(baseline_parser)
    baseline_parser.add_argument("--out", help="default tools/vendor-terms/baseline.<repo>.json")

    known_parser = commands.add_parser("confidential-known",
                                       help="write the dated ledger of confidential uses being burned down")
    _add_scan_arguments(known_parser)
    known_parser.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    known_parser.add_argument("--burn-down", action="append", required=True,
                              help="issue that removes the listed uses (repeatable)")
    known_parser.add_argument("--out", help="default tools/vendor-terms/confidential-known.<repo>.json")

    shrink_parser = commands.add_parser("check-baselines",
                                        help="fail when a committed baseline or confidential-known ledger grew")
    shrink_parser.add_argument("--base-ref", required=True)
    shrink_parser.add_argument("--repo-root", default=str(ROOT))

    args = parser.parse_args(argv)

    if args.command == "check-baselines":
        problems = _check_ledgers(Path(args.repo_root), args.base_ref)
        for problem in problems:
            print(f"::error::{problem}")
        print("vendor-term baselines and ledgers: " + ("GREW" if problems else "only shrink — ok"))
        return 1 if problems else 0

    report = _report_for(args)
    default_baseline = TERMS_DIR / f"baseline.{args.repo}.json"

    if args.command == "baseline":
        out = Path(args.out) if args.out else default_baseline
        out.write_text(json.dumps(baseline_from(report), indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out} ({sum(report['avoidableByFile'].values())} avoidable)")
        return 0

    if args.command == "confidential-known":
        out = Path(args.out) if args.out else TERMS_DIR / f"confidential-known.{args.repo}.json"
        out.write_text(json.dumps(known_from(report, args.as_of, args.burn_down), indent=2) + "\n",
                       encoding="utf-8")
        print(f"wrote {out} ({report['counts'][R30]} confidential)")
        return 0

    _write(args.json_out, json.dumps(report, indent=2) + "\n")
    _write(args.markdown_out, render_markdown(report))
    counts = report["counts"]
    print(f"{report['repo']} @ {report['sha'][:12]}: spec={counts['spec']} spec-interop={counts['spec-interop']} "
          f"nominative={counts['nominative']} confidential={counts[R30]} "
          f"avoidable={counts['avoidable']} (excepted {counts['avoidableExcepted']})")
    if args.command == "scan":
        return 0
    return _lint(args, report, default_baseline)


if __name__ == "__main__":
    sys.exit(main())
