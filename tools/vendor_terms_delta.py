"""Reject newly introduced vendor terminology without requiring a historical baseline."""
from __future__ import annotations

import argparse
import bisect
import html
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import check_vendor_terms as policy

MAX_BLOB_BYTES = 64 * 1024 * 1024

# Only recognized opaque assets are excluded. A filename suffix alone is not
# proof of binary content: executable source can be disguised as an image.
BINARY_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    '.png': (b'\x89PNG\r\n\x1a\n',), '.jpg': (b'\xff\xd8\xff',), '.jpeg': (b'\xff\xd8\xff',),
    '.gif': (b'GIF87a', b'GIF89a'), '.ico': (b'\x00\x00\x01\x00',),
    '.tif': (b'II*\x00', b'MM\x00*'), '.tiff': (b'II*\x00', b'MM\x00*'),
    '.pdf': (b'%PDF-',), '.woff': (b'wOFF',), '.woff2': (b'wOF2',),
    '.ttf': (b'\x00\x01\x00\x00', b'true'), '.otf': (b'OTTO',),
    '.zip': (b'PK\x03\x04', b'PK\x05\x06'), '.xlsx': (b'PK\x03\x04',),
    '.docx': (b'PK\x03\x04',), '.pptx': (b'PK\x03\x04',), '.npz': (b'PK\x03\x04',),
    '.gz': (b'\x1f\x8b',), '.tgz': (b'\x1f\x8b',), '.7z': (b"7z\xbc\xaf'\x1c",),
    '.parquet': (b'PAR1',), '.sqlite': (b'SQLite format 3\x00',),
    '.db': (b'SQLite format 3\x00',), '.gpkg': (b'SQLite format 3\x00',),
    '.glb': (b'glTF',), '.wasm': (b'\x00asm',), '.npy': (b'\x93NUMPY',),
}


def known_binary_asset(path: str, data: bytes) -> bool:
    if Path(path).suffix.lower() == '.webp':
        return data.startswith(b'RIFF') and data[8:12] == b'WEBP'
    return data.startswith(BINARY_SIGNATURES.get(Path(path).suffix.lower(), ()))


def binary_contains_visible_mark(data: bytes, classifier: policy.Classifier, path: str) -> bool:
    # Opaque assets are not fully inspectable. Screen plainly encoded text using
    # the same word boundaries and protocol exceptions as source files.
    names = ('es' + 'ri', 'arc' + 'gis', 'arc' + 'py', 'arc' + 'map', 'arc' + 'catalog',
             'arc' + 'objects', 'arc' + 'sde', 'arc' + 'ims', 'living' + ' ' + 'atlas')
    lowered = data.lower()
    for encoding in ('latin-1', 'utf-16-le', 'utf-16-be'):
        probe_encoding = 'ascii' if encoding == 'latin-1' else encoding
        if not any(name.encode(probe_encoding) in lowered for name in names):
            continue
        for offset in ((0,) if encoding == 'latin-1' else (0, 1)):
            decoded = data[offset:].decode(encoding, errors='ignore')
            # Non-ASCII binary bytes delimit words; they must not hide ASCII names.
            text = ''.join(char if ord(char) < 128 else ' ' for char in decoded)
            validate_scan_bounds(text)
            if any(hit.cls not in ('spec', 'spec-interop') and hit.exception is None
                   for hit in classifier.classify_text(path, text)):
                return True
            if classify_embedded_names(classifier, path, text):
                return True
    return False



def git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(['git', '-C', str(root), *args], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=check)


def blob(root: Path, revision: str, path: str) -> bytes | None:
    result = git(root, 'show', f'{revision}:{path}', check=False)
    if result.returncode:
        return None
    return result.stdout


def changed_lines(root: Path, base: str, head: str, path: str) -> tuple[set[int], bool]:
    diff = git(root, 'diff', '--no-renames', '--no-ext-diff', '--no-textconv', '--no-color', '--unified=0',
               base, head, '--', f':(literal){path}').stdout
    lines: set[int] = set()
    has_hunk = False
    for line in diff.splitlines():
        match = re.match(rb'^@@ -[0-9]+(?:,[0-9]+)? \+([0-9]+)(?:,([0-9]+))? @@', line)
        if match:
            has_hunk = True
            start = int(match.group(1))
            count = int(match.group(2)) if match.group(2) is not None else 1
            lines.update(range(start, start + count))
    return lines, has_hunk


def logical_to_git_lines(text: str) -> list[int]:
    # The classifier uses splitlines(), which splits more separators than Git's LF-based hunks.
    physical = 1
    mapping: list[int] = []
    for logical_line in text.splitlines(keepends=True):
        mapping.append(physical)
        physical += logical_line.count('\n')
    return mapping


# Distinctive product names remain recognizable inside lowercase compound names.
# The short vendor prefix is intentionally excluded: it appears in ordinary SRID names.
DISTINCTIVE = re.compile('|'.join(('arc' + 'gis', 'arc' + 'py', 'arc' + 'map',
                                   'arc' + 'catalog', 'arc' + 'objects', 'arc' + 'sde',
                                   'arc' + 'ims', r'living[ _-]*atlas',
                                   'es' + 'ri' + r'(?:client|adapter|layer|service|feature|compat|server|migration|portal|sdk|geometry|field)')), re.IGNORECASE)

ESCAPED_CHAR = re.compile(r'(?<!\\)(?P<slashes>\\+)(?:u\{(?P<braced>[0-9a-fA-F]{1,6})\}|u(?P<u4>[0-9a-fA-F]{4})|U(?P<u8>[0-9a-fA-F]{8})|x(?P<x2>[0-9a-fA-F]{2})|N\{(?P<name>[A-Za-z0-9 -]{1,100})\}|(?P<octal>[0-7]{1,3}))')


def decode_source_escapes(text: str) -> str:
    """Decode common ASCII source escapes without interpreting paired backslashes."""
    def replace(match: re.Match[str]) -> str:
        slashes = match.group('slashes')
        if len(slashes) % 2 == 0:
            return match.group()
        try:
            name = match.group('name')
            if name:
                char = unicodedata.lookup(name)
            elif match.group('octal'):
                char = chr(int(match.group('octal'), 8))
            else:
                char = chr(int(match.group('braced') or match.group('u4')
                               or match.group('u8') or match.group('x2'), 16))
        except (KeyError, ValueError, OverflowError):
            return match.group()
        if len(char) != 1 or ord(char) > 127:
            return match.group()
        return slashes[:-1] + char
    return ESCAPED_CHAR.sub(replace, text)


def decode_rendered_text(text: str) -> str:
    """Decode nested HTML and source escapes with a bounded number of passes."""
    for _ in range(8):
        decoded = html.unescape(text) if '&' in text else text
        decoded = decode_source_escapes(decoded) if '\\' in decoded else decoded
        if decoded == text:
            return text
        text = decoded
    raise RuntimeError('Candidate has too many nested encodings to inspect safely')


def validate_scan_bounds(text: str) -> None:
    # The shared classifier performs overlap checks on candidate spans. Bound
    # untrusted input so an adversarial line cannot stall a required CI check.
    if len(text) > 16 * 1024 * 1024:
        raise RuntimeError('Candidate text exceeds the inspection limit')
    regex_work = 0
    for line in text.splitlines():
        decoded = decode_rendered_text(line)
        if not any(pattern.search(value) for pattern in (policy.PRESCREEN, DISTINCTIVE)
                   for value in (line, decoded)):
            continue
        if len(line) > 1024 * 1024:
            raise RuntimeError('Candidate line exceeds the inspection limit')
        # The policy's URL recognizer operates on an enclosing word. Bound word
        # size in a single pass to prevent adversarial quadratic regex work.
        word_length = 0
        for character in line:
            if character.isspace() or character in "\"'()[]{}<>,;|":
                word_length = 0
            else:
                word_length += 1
                if word_length > 4096:
                    raise RuntimeError('Candidate token exceeds the inspection limit')
        count = 0
        for candidate in (line,) if decoded == line else (line, decoded):
            for pattern in (policy.PRESCREEN, DISTINCTIVE):
                for _ in pattern.finditer(candidate):
                    count += 1
                    if count > 512:
                        raise RuntimeError('Candidate line has too many terms to inspect safely')
            # Both raw and decoded text reach the classifier. Bound their
            # aggregate quadratic URL-search cost across the whole candidate.
            for word in re.finditer(r'[^\s"\'()\[\]{}<>,;|]+', candidate):
                value = word.group()
                matches = sum(1 for pattern in (policy.PRESCREEN, DISTINCTIVE)
                              for _ in pattern.finditer(value))
                if matches:
                    regex_work += len(value) ** 2 * matches
                    if regex_work > 2_000_000:
                        raise RuntimeError('Candidate requires too much inspection work')


def classify_embedded_names(classifier: policy.Classifier, path: str, text: str) -> list[policy.Hit]:
    findings: list[policy.Hit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        matches = list(DISTINCTIVE.finditer(line))
        if not matches:
            continue
        spans = policy._spans(line)
        identifiers = list(re.finditer(r'[A-Za-z0-9_]+', line))
        starts = [item.start() for item in identifiers]
        visited: set[tuple[int, int]] = set()
        for match in matches:
            if any(start <= match.start() and end >= match.end()
                   for start, end, *_ in spans):
                continue
            left_index = bisect.bisect_right(starts, match.start()) - 1
            right_index = bisect.bisect_right(starts, match.end() - 1) - 1
            left = identifiers[left_index].start() if left_index >= 0 else match.start()
            right = identifiers[right_index].end() if right_index >= 0 else match.end()
            bounds = (left, right)
            if bounds in visited:
                continue
            visited.add(bounds)
            identifier = line[left:right]
            if identifier in classifier.vocabulary or identifier in classifier.interop.identifiers:
                continue
            exception = classifier._exception(path, identifier, identifier, 'identifier')
            if exception is None:
                findings.append(policy.Hit(path, number, identifier, 'embedded-name',
                                           'avoidable', 'embedded-identifier'))
    return findings


def classify_decoded_text(classifier: policy.Classifier, path: str, text: str) -> list[policy.Hit]:
    findings: list[policy.Hit] = []
    # Split only on LF to retain the line numbers reported by Git's diff hunks.
    for physical_line, line in enumerate(text.split('\n'), start=1):
        if '&' not in line and '\\' not in line:
            continue
        decoded = decode_rendered_text(line)
        if decoded == line:
            continue
        validate_scan_bounds(decoded)
        hits = classifier.classify_text(path, decoded)
        hits += classify_fence_labels(classifier, path, decoded)
        hits += classify_embedded_names(classifier, path, decoded)
        for hit in hits:
            if hit.cls not in ('spec', 'spec-interop') and hit.exception is None:
                hit.line = physical_line
                findings.append(hit)
    return findings


def classify_fence_labels(classifier: policy.Classifier, path: str, text: str) -> list[policy.Hit]:
    if Path(path).suffix.lower() not in policy.PROSE_SUFFIXES:
        return []
    findings: list[policy.Hit] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith(('```', '~~~')) and policy.PRESCREEN.search(line):
            for hit in classifier.classify_text(path, 'language ' + line):
                if hit.cls not in ('spec', 'spec-interop') and hit.exception is None:
                    hit.line = number
                    findings.append(hit)
    return findings


def find_new_hits(root: Path, repo: str, base: str, head: str) -> list[policy.Hit]:
    # Reject bad references before querying individual paths; an invalid base must never pass as an empty tree.
    for revision in (base, head):
        git(root, 'rev-parse', '--verify', f'{revision}^{{commit}}')
    raw_paths = git(root, 'diff', '--no-renames', '--name-only', '-z', '--diff-filter=ACMRT', base, head).stdout
    paths = [p.decode('utf-8', errors='surrogateescape') for p in raw_paths.split(b'\0') if p]
    old_raw = git(root, 'ls-tree', '-r', '--name-only', '-z', base).stdout
    old_prefixes: set[str] = set()
    for raw in old_raw.split(b'\0'):
        if not raw:
            continue
        parts = raw.decode('utf-8', errors='surrogateescape').split('/')
        old_prefixes.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    classifier = policy.Classifier(repo, policy.load_vocabulary(), policy.load_allowlist(policy.ALLOWLIST),
                                   policy.load_interop(), skip_encoded=False)
    findings: list[policy.Hit] = []
    seen_paths: set[str] = set()
    for path in paths:
        entry = git(root, 'ls-tree', '-z', head, '--', f':(literal){path}').stdout
        if not entry:
            raise RuntimeError(f'Cannot inspect changed path metadata: {path!r}')
        if entry.startswith(b'160000 '):
            raise RuntimeError(f'Cannot inspect changed submodule content: {path!r}')
        parts = path.split('/')
        for i, part in enumerate(parts, start=1):
            prefix = '/'.join(parts[:i])
            if prefix not in old_prefixes and prefix not in seen_paths:
                for hit in classify_embedded_names(classifier, prefix, part):
                    hit.line = 0
                    findings.append(hit)
                    seen_paths.add(prefix)
        for hit in classifier.classify_path(path):
            if hit.path not in old_prefixes and hit.path not in seen_paths and hit.exception is None:
                findings.append(hit)
                seen_paths.add(hit.path)
        size = int(git(root, 'cat-file', '-s', f'{head}:{path}').stdout.strip())
        if size > MAX_BLOB_BYTES:
            raise RuntimeError(f'Changed blob exceeds the inspection limit: {path!r}')
        new = blob(root, head, path)
        if new is None:
            raise RuntimeError(f'Cannot read changed path at head: {path!r}')
        if re.match(rb'^(?:\xef\xbb\xbf)?version https://git-lfs\.github\.com/spec/v1(?:\r?\n|$)', new):
            raise RuntimeError(f'Cannot inspect Git LFS object content: {path!r}')
        if b'\0' in new:
            if not known_binary_asset(path, new):
                raise RuntimeError(f'Cannot inspect unknown binary content: {path!r}')
            if binary_contains_visible_mark(new, classifier, path):
                raise RuntimeError(f'Binary asset contains a visible vendor term: {path!r}')
            continue  # Compressed and visual content is outside the text classifier.
        new_text = new.decode('utf-8', errors='replace')
        if (not policy.PRESCREEN.search(new_text) and not DISTINCTIVE.search(new_text)
                and '&' not in new_text and '\\' not in new_text):
            continue
        lines, has_hunk = changed_lines(root, base, head, path)
        if not has_hunk and blob(root, base, path) != new:
            raise RuntimeError(f'Cannot inspect changed text lines: {path!r}')
        changed_text = '\n'.join(line for number, line in enumerate(new_text.split('\n'), 1)
                                 if number in lines)
        if len(changed_text) > 16 * 1024 * 1024:
            raise RuntimeError('Changed text exceeds the inspection limit')
        decoded_changed = decode_rendered_text(changed_text)
        if not any(pattern.search(value) for pattern in (policy.PRESCREEN, DISTINCTIVE)
                   for value in (changed_text, decoded_changed)):
            continue
        validate_scan_bounds(new_text)
        physical_lines = logical_to_git_lines(new_text)
        for hit in classifier.classify_text(path, new_text):
            if (physical_lines[hit.line - 1] in lines and hit.cls not in ('spec', 'spec-interop')
                    and hit.exception is None):
                findings.append(hit)
        findings.extend(hit for hit in classify_embedded_names(classifier, path, new_text)
                        if physical_lines[hit.line - 1] in lines)
        findings.extend(hit for hit in classify_decoded_text(classifier, path, new_text)
                        if hit.line in lines)
        # Markdown fence labels are visible text even though the shared classifier skips delimiters.
        findings.extend(hit for hit in classify_fence_labels(classifier, path, new_text)
                        if physical_lines[hit.line - 1] in lines)

    return findings


def find_pr_metadata_hits(repo: str, payload: dict) -> list[policy.Hit]:
    pr = payload.get('pull_request')
    if not isinstance(pr, dict) or not isinstance(pr.get('title'), str):
        raise RuntimeError('Pull request metadata is missing or invalid')
    body = pr.get('body')
    if body is None:
        body = ''
    if not isinstance(body, str):
        raise RuntimeError('Pull request description is invalid')
    classifier = policy.Classifier(repo, policy.load_vocabulary(), [], policy.load_interop(), skip_encoded=False)
    findings: list[policy.Hit] = []
    for path, text in (('pull-request-title.md', pr['title']), ('pull-request-body.md', body)):
        validate_scan_bounds(text)
        findings.extend(hit for hit in classifier.classify_text(path, text)
                        if hit.cls not in ('spec', 'spec-interop'))
        findings.extend(classify_fence_labels(classifier, path, text))
        findings.extend(classify_embedded_names(classifier, path, text))
        findings.extend(classify_decoded_text(classifier, path, text))
    return findings


def annotation_property(value: str) -> str:
    # Paths come from the proposed tree, so they must not inject workflow commands.
    return (value.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
            .replace(':', '%3A').replace(',', '%2C'))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--repo', required=True)
    parser.add_argument('--base', required=True)
    parser.add_argument('--head', required=True)
    parser.add_argument('--event-name', default='')
    parser.add_argument('--event', type=Path)
    args = parser.parse_args()
    try:
        findings = find_new_hits(args.root, args.repo, args.base, args.head)
        if args.event_name in ('pull_request', 'pull_request_target'):
            if args.event is None:
                raise RuntimeError('Pull request event payload is required')
            findings.extend(find_pr_metadata_hits(args.repo, json.loads(args.event.read_text(encoding='utf-8'))))
        elif args.event_name and args.event_name != 'merge_group':
            raise RuntimeError('Unsupported event type')
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError):
        # Error strings can include confidential or untrusted paths and command arguments.
        print('vendor guard could not inspect all changed content; review the candidate locally', file=sys.stderr)
        return 2
    for hit in findings:
        # Do not echo marked text or confidential values into public CI logs.
        if policy.is_confidential_text(hit.path) or policy.PRESCREEN.search(hit.path) or DISTINCTIVE.search(hit.path):
            print('::error::New vendor term in a path; path withheld from public logs')
        else:
            print(f'::error file={annotation_property(hit.path)},line={max(hit.line, 1)}::'
                  f'New vendor term ({hit.cls}; {hit.category})')
    print(f'vendor guard: {len(findings)} new finding(s)')
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
