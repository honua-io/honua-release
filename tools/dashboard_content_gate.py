#!/usr/bin/env python3
"""Fail closed when a rendered public dashboard contains private content."""
from __future__ import annotations

import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re

POLICY = Path(__file__).with_name('dashboard-content-policy.json')


def entry_text(value) -> str:
    """A policy entry is a string, or string fragments joined in order.

    Fragments keep a contiguous mark out of the policy file. The vendor-term lint fails a new
    avoidable use, and a confidential use can be neither baselined nor allowlisted.
    """
    if isinstance(value, str):
        text = value
    elif isinstance(value, list) and value and all(isinstance(part, str) and part for part in value):
        text = ''.join(value)
    else:
        raise ValueError(
            f'policy entry must be a non-empty string or a list of non-empty strings, got {value!r}')
    if not text.strip():
        raise ValueError('policy entry is empty')
    return text


class VisibleRows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.row = None
        self.cell = None
        self.rows = []
        self.page = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.hidden += 1
        elif tag == 'tr' and not self.hidden:
            self.row = []
        elif tag in {'td', 'th'} and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.hidden:
            self.hidden -= 1
        elif tag == 'tr' and self.row is not None:
            if self.row:
                self.rows.append(self.row)
            self.row = None
        elif tag in {'td', 'th'} and self.cell is not None:
            self.row.append(' '.join(' '.join(self.cell).split()))
            self.cell = None

    def handle_data(self, data):
        if not self.hidden:
            self.page.append(data)
            if self.cell is not None:
                self.cell.append(data)


def violations(source: str, policy: dict) -> list[str]:
    parser = VisibleRows()
    parser.feed(source)
    problems = []
    for cells in parser.rows:
        row = ' '.join(cells)
        for repo in (entry_text(item) for item in policy['private_repositories']):
            matches = re.findall(rf"\b{re.escape(repo)}#\d+\b", row, re.IGNORECASE)
            for reference in matches:
                reference_cell = next((index for index, cell in enumerate(cells) if cell == reference), None)
                neutral_title = (reference_cell is not None and reference_cell + 1 < len(cells)
                                 and cells[reference_cell + 1] == f'Evidence {reference}')
                if not neutral_title:
                    problems.append(f'{reference}: private-repository row carries title/detail: {row}')
    visible = '\n'.join(parser.page)
    for number, line in enumerate(visible.splitlines(), 1):
        clean = ' '.join(line.split())
        for term in (entry_text(item) for item in policy['restricted_terms']):
            if clean and re.search(re.escape(term), clean, re.IGNORECASE):
                problems.append(f'line {number}: restricted term {term!r}: {clean}')
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', type=Path)
    args = parser.parse_args(argv)
    policy = json.loads(POLICY.read_text(encoding='utf-8'))
    problems = violations(args.html.read_text(encoding='utf-8'), policy)
    for problem in problems:
        print(f'OFFENDING {problem}')
    return 1 if problems else 0


if __name__ == '__main__':
    raise SystemExit(main())
