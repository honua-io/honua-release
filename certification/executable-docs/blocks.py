"""Extract and classify the fenced blocks of a getting-started document.

Shared by inventory.py (what the docs ask a reader to run) and run.py (running it). Everything here
is pure text processing so it can be unit-tested without a network, Docker or a server.

Intent of a block:
  run          the reader is meant to run it (shell, python, js/ts, csharp, http)
  compile      the author declared it compile-only (`doc-test=compile`); the oracle is a typecheck
  file         the reader is meant to save it as a file (a save/create cue names the file)
  output       expected output of the preceding run block; used as that block's oracle
  alternative  a platform variant of a neighbouring block (PowerShell, cmd) this Linux lane cannot run
  illustrative a language this gate does not execute (json, yaml, html, text, ...)
  excluded     the author marked it `<!-- doc-run: skip reason="..." -->` or `doc-test=skip reason=...`
  teardown     stops what the reader started (`docker compose down`); run when the reader is done with
               the session, after any document that continues this one
"""
from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field, asdict
from typing import Any

RUN_LANGUAGES = {
    "bash": "shell", "sh": "shell", "shell": "shell", "zsh": "shell", "console": "shell",
    "shell-session": "shell", "shellsession": "shell", "terminal": "shell",
    "python": "python", "py": "python", "python3": "python", "pycon": "python",
    "js": "javascript", "javascript": "javascript", "mjs": "javascript", "node": "javascript",
    "ts": "typescript", "typescript": "typescript", "mts": "typescript",
    "csharp": "csharp", "cs": "csharp", "c#": "csharp",
    "http": "http",
}
ALTERNATIVE_LANGUAGES = {"powershell", "pwsh", "ps1", "ps", "cmd", "bat", "batch"}
OUTPUT_LANGUAGES = {"", "text", "txt", "output", "plaintext", "plain", "console-output", "log", "json", "jsonc"}
OUTPUT_CUE = re.compile(
    r"\b(output|outputs|prints?|printed|returns?|response|responds|you (?:should |will |'ll )?see|"
    r"expect(?:ed)?|looks? like|result(?:s)?|shows?|answers?)\b", re.I)
FILE_NAME = re.compile(
    r"`((?:[\w.-]+/)*(?:[\w.-]+\.(?:py|js|mjs|cjs|ts|mts|tsx|cs|csproj|json|jsonc|ya?ml|toml|env|html|sh|"
    r"txt|sql|xml|config|ini|props)|\.env|Dockerfile|Program\.cs))`")
FILE_CUE = re.compile(
    r"\b(save|saved|saving|put|create|creating|write|writing|named|called|paste|contents of|file)\b", re.I)
COMMAND_START = re.compile(
    r"^(\$ |npm |npx |pnpm |yarn |pip |pip3 |python3? |uv |dotnet |docker |curl |git |cd |export |honua |"
    r"node |mkdir |cat |source |set )")
TEARDOWN_LINE = re.compile(r"^\s*(?:#.*|docker\s+compose\s+(?:-f\s+\S+\s+)*(?:down|stop|rm)\b.*|docker\s+(?:stop|rm)\b.*|)$")
EXPECT_FAILURE = re.compile(r"\bdeliberately (?:broken|invalid|bad|wrong)\b|\bto see it (?:catch|fail|reject|refuse)", re.I)
DOC_RUN = re.compile(r"<!--\s*doc-run:\s*(.*?)\s*-->", re.S)
ATTR = re.compile(r'([\w-]+)(?:=(?:"([^"]*)"|\'([^\']*)\'|(\S+)))?')


@dataclass
class Block:
    index: int
    line: int
    language: str             # normalized language, or the raw info word for non-run languages
    raw_language: str
    code: str
    intent: str = "illustrative"
    intent_source: str = "heuristic"
    reason: str = ""
    file: str | None = None
    output_of: int | None = None
    expected_output: str | None = None
    marker_error: str | None = None
    expect_failure: bool = False
    preceding_text: str = field(default="", repr=False)
    info_attrs: dict[str, str] = field(default_factory=dict, repr=False)
    marker: dict[str, str] | None = field(default=None, repr=False)

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.code.encode("utf-8")).hexdigest()

    def record(self) -> dict[str, Any]:
        row = {
            "index": self.index, "line": self.line, "language": self.language,
            "infoString": self.raw_language, "intent": self.intent, "intentSource": self.intent_source,
            "sha256": self.sha256,
        }
        if self.expect_failure:
            row["expectFailure"] = True
        for key, value in (("reason", self.reason), ("file", self.file), ("outputOf", self.output_of),
                           ("markerError", self.marker_error)):
            if value not in (None, ""):
                row[key] = value
        return row


def _attrs(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for match in ATTR.finditer(text):
        key = match.group(1)
        value = next((g for g in match.group(2, 3, 4) if g is not None), "")
        out[key] = value
    return out


def _dedent(lines: list[str], indent: int) -> list[str]:
    return [line[indent:] if line[:indent].strip() == "" else line.lstrip() for line in lines]


def parse_markdown(text: str) -> list[Block]:
    """Fenced code blocks with the prose between them; fences may be indented inside list items."""
    lines = text.splitlines()
    blocks: list[Block] = []
    prose: list[str] = []
    i = 0
    in_front_matter = bool(lines) and lines[0].strip() == "---"
    if in_front_matter:
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^((?:\s*>)*)(\s*)(`{3,}|~{3,})(.*)$", line)
        if not match:
            prose.append(line)
            i += 1
            continue
        quote, indent, fence, info = match.group(1), len(match.group(2)), match.group(3), match.group(4).strip()
        depth = quote.count(">")
        body: list[str] = []
        i += 1
        while i < len(lines):
            current = lines[i]
            for _ in range(depth):   # a fence inside a blockquote: strip the quote markers
                current = re.sub(r"^\s*> ?", "", current, count=1)
            close = re.match(r"^\s*(`{3,}|~{3,})\s*$", current)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                break
            body.append(current)
            i += 1
        start_line = i - len(body)
        i += 1
        words = info.split(None, 1)
        raw = words[0].lower() if words else ""
        raw = raw.strip("{}.")
        attrs = _attrs(words[1]) if len(words) > 1 else {}
        block = Block(index=len(blocks), line=start_line, language=RUN_LANGUAGES.get(raw, raw),
                      raw_language=info, code="\n".join(_dedent(body, indent)).rstrip("\n") + "\n",
                      preceding_text="\n".join(prose), info_attrs=attrs)
        prose = []
        blocks.append(block)
    return blocks


PRE = re.compile(r"<pre\b([^>]*)>(.*?)</pre>", re.S | re.I)


def parse_html(text: str) -> list[Block]:
    """<pre> blocks of a published HTML page. Spans are presentation; a `comment` span is a comment."""
    blocks: list[Block] = []
    last = 0
    for match in PRE.finditer(text):
        attrs, inner = match.group(1), match.group(2)
        code_match = re.search(r"<code\b([^>]*)>(.*)</code>", inner, re.S | re.I)
        if code_match:
            attrs, inner = attrs + " " + code_match.group(1), code_match.group(2)
        lang = ""
        cls = re.search(r'class="([^"]*)"', attrs)
        if cls:
            found = re.search(r"(?:language|lang)-([\w#+-]+)", cls.group(1))
            lang = found.group(1).lower() if found else ""
        stripped = re.sub(r"<[^>]+>", "", inner)
        code = html.unescape(stripped).strip("\n") + "\n"
        before = html.unescape(re.sub(r"<[^>]+>", " ", text[last:match.start()]))
        last = match.end()
        if not lang:
            first = next((line for line in code.splitlines() if line.strip() and not line.startswith("#")), "")
            lang = "bash" if COMMAND_START.match(first.strip()) else ""
        line = text.count("\n", 0, match.start()) + 1
        blocks.append(Block(index=len(blocks), line=line, language=RUN_LANGUAGES.get(lang, lang),
                            raw_language=lang, code=code, preceding_text=before))
    return blocks


def _markers(prose: str) -> dict[str, str] | None:
    """The doc-run marker that immediately precedes a block (only blank lines may separate them)."""
    tail = prose.rstrip()
    found = None
    for match in DOC_RUN.finditer(tail):
        if tail[match.end():].strip() == "":
            found = match
    return _attrs(found.group(1)) if found else None


def _last_paragraph(prose: str) -> str:
    parts = [p for p in re.split(r"\n\s*\n", prose.strip()) if p.strip()]
    return parts[-1] if parts else ""


def split_console(code: str) -> tuple[str, str]:
    """`$ cmd` lines are commands; the remaining lines of a console transcript are its output."""
    commands, output = [], []
    for line in code.splitlines():
        if line.startswith("$ "):
            commands.append(line[2:])
        elif commands and commands[-1].endswith("\\"):
            commands.append(line)
        else:
            output.append(line)
    return "\n".join(commands) + "\n", "\n".join(output).strip()


def classify(blocks: list[Block]) -> list[Block]:
    previous_run: Block | None = None
    for block in blocks:
        marker = _markers(block.preceding_text)
        block.marker = marker
        info = block.info_attrs
        paragraph = _last_paragraph(re.sub(DOC_RUN, "", block.preceding_text))
        block.expect_failure = bool((marker and "expect-fail" in marker) or EXPECT_FAILURE.search(paragraph))
        # 1. explicit author declarations win
        if marker is not None:
            block.intent_source = "marker"
            if "skip" in marker:
                if marker.get("reason", "").strip():
                    block.intent, block.reason = "excluded", marker["reason"].strip()
                    continue
                block.marker_error = "doc-run: skip without a reason is ignored; the block still runs"
                block.intent_source = "heuristic"
            elif "file" in marker and marker["file"]:
                block.intent, block.file = "file", marker["file"]
                continue
            elif "teardown" in marker:
                block.intent = "teardown"
                continue
            elif "output" in marker:
                block.intent = "output"
                if previous_run is not None:
                    block.output_of = previous_run.index
                    previous_run.expected_output = block.code.strip()
                continue
            elif "run" in marker or "checkout" in marker or "expect-fail" in marker:
                block.intent = "run"
                if block.language not in set(RUN_LANGUAGES.values()):
                    block.marker_error = f"doc-run: run on a language this gate cannot execute ({block.language})"
                    block.intent = "illustrative"
                previous_run = block if block.intent == "run" else previous_run
                continue
        doc_test = info.get("doc-test")
        if doc_test == "skip":
            block.intent_source = "fence-attribute"
            if info.get("reason", "").strip():
                block.intent, block.reason = "excluded", info["reason"].strip()
                continue
            block.marker_error = "doc-test=skip without a reason is ignored; the block still runs"
        if doc_test == "compile" and block.language in {"typescript", "javascript"}:
            block.intent, block.intent_source = "compile", "fence-attribute"
            previous_run = None
            continue
        # 2. heuristics
        lang = block.language
        if lang in ALTERNATIVE_LANGUAGES:
            block.intent = "alternative"
            block.reason = f"{lang} is a Windows variant; this lane runs the Linux/macOS instructions"
            continue
        file_match = FILE_NAME.findall(paragraph)
        if file_match and FILE_CUE.search(paragraph) and not (lang == "shell" and block.code.lstrip().startswith("$ ")):
            name = file_match[-1]
            ext_lang = {"py": "python", "js": "javascript", "mjs": "javascript", "cjs": "javascript",
                        "ts": "typescript", "mts": "typescript", "cs": "csharp", "sh": "shell"}
            ext = name.rsplit(".", 1)[-1] if "." in name.lstrip(".") else ""
            if lang not in RUN_LANGUAGES.values() or ext_lang.get(ext) == lang:
                block.intent, block.file = "file", name
                previous_run = None
                continue
        if lang == "shell" and block.code.strip() and all(TEARDOWN_LINE.match(l) for l in block.code.splitlines()):
            block.intent = "teardown"
            block.reason = "stops the reader's stack; run when the session's documents are done"
            previous_run = None
            continue
        if lang == "shell":
            if block.raw_language.split()[0].lower() in {"console", "shell-session", "shellsession"} \
                    or (block.code.lstrip().startswith("$ ") and any(
                        not l.startswith("$ ") and l.strip() for l in block.code.splitlines())):
                commands, output = split_console(block.code)
                block.code = commands
                if output:
                    block.expected_output = output
            block.intent = "run"
            previous_run = block
            continue
        if lang in {"python", "javascript", "typescript", "csharp", "http"}:
            block.intent = "run"
            previous_run = block
            continue
        if lang in OUTPUT_LANGUAGES and previous_run is not None and previous_run.index == block.index - 1:
            gap = [line for line in block.preceding_text.splitlines() if line.strip()]
            # "It prints:" directly under the command, or no prose at all: that is the command's output.
            if not gap or (len(gap) <= 3 and gap[-1].rstrip().endswith(":") and OUTPUT_CUE.search(" ".join(gap))):
                block.intent, block.output_of = "output", previous_run.index
                previous_run.expected_output = block.code.strip()
                previous_run = None
                continue
        if lang == "":
            first = next((l.strip() for l in block.code.splitlines() if l.strip()), "")
            if COMMAND_START.match(first):
                block.language, block.intent = "shell", "run"
                block.code = split_console(block.code)[0] if first.startswith("$ ") else block.code
                previous_run = block
                continue
        block.intent = "illustrative"
        block.reason = f"{lang or 'unlabelled'} block is not executable by this gate"
    return blocks


def extract(text: str, fmt: str) -> list[Block]:
    return classify(parse_html(text) if fmt == "html" else parse_markdown(text))


def records(blocks: list[Block]) -> list[dict[str, Any]]:
    return [block.record() for block in blocks]


__all__ = ["Block", "extract", "records", "parse_markdown", "parse_html", "classify", "split_console", "asdict"]
