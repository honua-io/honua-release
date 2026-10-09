#!/usr/bin/env python3
"""MCP tool-roster parity gate (honua-server#5734, spec mcp-tool-roster-and-parity-gates).

e2e/drivers/mcp/expected-tools.json is a hand-regenerated snapshot of the MCP discovery surface of the
honua-server image pinned in platform-manifest.yaml. honua-server publishes the canonical roster as a
generated artifact, docs/gis/data/mcp-tool-roster.v1.json (schema mcp-tool-roster.v1.schema.json,
guarded on the server side by McpToolRosterDriftTests). This gate reads that artifact AT THE PINNED
honua-server sha and fails when the snapshot has drifted from it:

  fullCatalog.tools                     == roster static | projectedAdmin   (missing / extra)
  defaultView.tools                     == roster views.default             (defaultMissing / defaultExtra)
  fullCatalog.requiresDurableControlPlane.tools
      redis-on  (default)               subset of roster requiresDurableControlPlane
      redis-off                         == roster requiresDurableControlPlane (the set the driver may
                                           accept as absent is the server's, not a hand-kept guess)
  no roster `retired` name anywhere in the expected lists                   (retiredSeen)
  roster schemaVersion                  == 1

Roster membership at the PINNED sha is the truth. The roster's own `serverSha` is the commit that last
changed its body, so it is reported but never compared with the pin. When the pinned sha has no roster
(HTTP 404, i.e. the pin predates honua-server#5742) the gate reports BLOCKED -- it never passes on an
absent artifact (AGENTS.md: a gate that cannot fail is worse than no gate).

Exit codes: 0 pass, 1 fail, 2 usage / IO error, 3 blocked. A JSON summary is always printed on stdout
(`status`, `serverSha`, `missing`, `extra`, `durableMismatch`, `retiredSeen`, ...); human-readable
reasons go to stderr.

  python tools/check_mcp_roster.py [--manifest platform-manifest.yaml]
      [--expected e2e/drivers/mcp/expected-tools.json] [--roster PATH|URL] [--server-sha SHA]
      [--topology redis-on|redis-off]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_REPOSITORY = 'honua-io/honua-server'
ROSTER_PATH = 'docs/gis/data/mcp-tool-roster.v1.json'
RAW_URL = 'https://raw.githubusercontent.com/{repository}/{sha}/{path}'
SCHEMA_VERSION = 1
TOPOLOGIES = ('redis-on', 'redis-off')
EXIT = {'pass': 0, 'fail': 1, 'error': 2, 'blocked': 3}
SHA_RE = re.compile(r'^[0-9a-f]{40}$')


class UsageError(Exception):
    """Bad arguments or unreadable input: exit 2, never a verdict."""


class RosterNotPublished(Exception):
    """The roster does not exist at the requested sha (HTTP 404): blocked."""


def pinned_server_sha(manifest_path: Path) -> str:
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        raise UsageError(f'cannot read manifest {manifest_path}: {exc}') from exc
    sha = (((manifest or {}).get('components') or {}).get('honua-server') or {}).get('sha')
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        raise UsageError(f'{manifest_path}: components.honua-server.sha is not a 40-hex sha ({sha!r})')
    return sha


def roster_url(sha: str) -> str:
    return RAW_URL.format(repository=SERVER_REPOSITORY, sha=sha, path=ROSTER_PATH)


def _fetch(url: str, opener) -> bytes:
    headers = {'User-Agent': 'honua-release/check_mcp_roster'}
    token = os.environ.get('RELEASE_GH_TOKEN') or os.environ.get('GH_TOKEN')
    if token:
        headers['Authorization'] = f'token {token}'
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RosterNotPublished(url) from exc
        raise UsageError(f'GET {url} failed: HTTP {exc.code}') from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UsageError(f'GET {url} failed: {exc}') from exc


def load_roster(source: str, opener=urllib.request.urlopen) -> dict:
    if re.match(r'^https?://', source):
        raw = _fetch(source, opener)
    else:
        try:
            raw = Path(source).read_bytes()
        except OSError as exc:
            raise UsageError(f'cannot read roster {source}: {exc}') from exc
    try:
        roster = json.loads(raw.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UsageError(f'roster {source} is not JSON: {exc}') from exc
    if not isinstance(roster, dict):
        raise UsageError(f'roster {source} is not a JSON object')
    return roster


def load_expected(path: Path) -> dict:
    try:
        expected = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f'cannot read expected tools {path}: {exc}') from exc
    if not isinstance(expected, dict):
        raise UsageError(f'{path} is not a JSON object')
    return expected


def _names(value, where: str) -> list:
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise ValueError(f'{where} is not a list of tool names')
    return value


def _expected_durable(full_catalog: dict) -> list:
    # honua-release#490 shape: {"_comment": ..., "tools": [...]}; a bare list is accepted too. Absent
    # means the snapshot declares no durable-control-plane subset.
    durable = full_catalog.get('requiresDurableControlPlane')
    if durable is None:
        return []
    if isinstance(durable, dict):
        durable = durable.get('tools')
    return _names(durable, 'expected fullCatalog.requiresDurableControlPlane.tools')


def check(expected: dict, roster: dict, *, topology: str = 'redis-on', server_sha: str = '') -> dict:
    """Pure comparison. Returns the summary dict; `status` is pass or fail."""
    if topology not in TOPOLOGIES:
        raise UsageError(f'unknown topology {topology!r}')
    summary = {
        'status': 'fail', 'serverSha': server_sha, 'rosterServerSha': roster.get('serverSha'),
        'topology': topology, 'missing': [], 'extra': [], 'defaultMissing': [], 'defaultExtra': [],
        'durableMismatch': [], 'retiredSeen': [], 'reasons': [],
    }
    reasons = summary['reasons']

    version = roster.get('schemaVersion')
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        reasons.append(f'roster schemaVersion is {version!r}, this gate reads version {SCHEMA_VERSION}')
        return summary
    try:
        static = _names(roster.get('static'), 'roster static')
        projected = _names(roster.get('projectedAdmin'), 'roster projectedAdmin')
        roster_durable = _names(roster.get('requiresDurableControlPlane'), 'roster requiresDurableControlPlane')
        roster_default = _names((roster.get('views') or {}).get('default'), 'roster views.default')
        retired = _names(roster.get('retired'), 'roster retired')
    except ValueError as exc:
        reasons.append(f'malformed roster: {exc}')
        return summary
    try:
        full_catalog = expected.get('fullCatalog') or {}
        default_view = expected.get('defaultView') or {}
        expected_full = _names(full_catalog.get('tools'), 'expected fullCatalog.tools')
        expected_default = _names(default_view.get('tools'), 'expected defaultView.tools')
        expected_durable = _expected_durable(full_catalog)
        stage_names = []
        for stage in default_view.get('stages') or []:
            stage_names += _names(stage.get('tools'), f"expected defaultView stage {stage.get('id')}")
        critical = _names(expected.get('criticalTools', []), 'expected criticalTools')
    except (ValueError, AttributeError) as exc:
        reasons.append(f'malformed expected tools: {exc}')
        return summary

    canonical = set(static) | set(projected)
    summary['missing'] = sorted(canonical - set(expected_full))
    summary['extra'] = sorted(set(expected_full) - canonical)
    if summary['missing']:
        reasons.append(f"{len(summary['missing'])} roster tool(s) missing from fullCatalog.tools: "
                       + ', '.join(summary['missing']))
    if summary['extra']:
        reasons.append(f"{len(summary['extra'])} fullCatalog.tools name(s) not in the roster: "
                       + ', '.join(summary['extra']))

    summary['defaultMissing'] = sorted(set(roster_default) - set(expected_default))
    summary['defaultExtra'] = sorted(set(expected_default) - set(roster_default))
    if summary['defaultMissing'] or summary['defaultExtra']:
        reasons.append('defaultView.tools differs from roster views.default (missing: '
                       f"{summary['defaultMissing']}, extra: {summary['defaultExtra']})")

    not_in_roster = set(expected_durable) - set(roster_durable)
    not_in_expected = set(roster_durable) - set(expected_durable) if topology == 'redis-off' else set()
    summary['durableMismatch'] = sorted(not_in_roster | not_in_expected)
    if not_in_roster:
        reasons.append('requiresDurableControlPlane name(s) the roster does not mark durable: '
                       + ', '.join(sorted(not_in_roster)))
    if not_in_expected:
        reasons.append('redis-off: roster durable-control-plane name(s) missing from the expected '
                       'allowed-absent set: ' + ', '.join(sorted(not_in_expected)))

    every_expected = (set(expected_full) | set(expected_default) | set(expected_durable)
                      | set(stage_names) | set(critical))
    summary['retiredSeen'] = sorted(every_expected & set(retired))
    if summary['retiredSeen']:
        reasons.append('retired tool name(s) still expected: ' + ', '.join(summary['retiredSeen']))

    if not reasons:
        summary['status'] = 'pass'
    return summary


def main(argv=None, opener=urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description='Gate expected-tools.json on the honua-server MCP roster.')
    parser.add_argument('--manifest', default=str(REPO_ROOT / 'platform-manifest.yaml'))
    parser.add_argument('--expected', default=str(REPO_ROOT / 'e2e' / 'drivers' / 'mcp' / 'expected-tools.json'))
    parser.add_argument('--roster', help='roster path or URL (default: the artifact at the pinned server sha)')
    parser.add_argument('--server-sha', help='override the pinned honua-server sha')
    parser.add_argument('--topology', choices=TOPOLOGIES, default='redis-on')
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT['error'] if exc.code else 0

    sha = ''
    try:
        if args.server_sha:
            if not SHA_RE.match(args.server_sha):
                raise UsageError(f'--server-sha {args.server_sha!r} is not a 40-hex sha')
            sha = args.server_sha
        else:
            sha = pinned_server_sha(Path(args.manifest))
        expected = load_expected(Path(args.expected))
        roster = load_roster(args.roster or roster_url(sha), opener)
        summary = check(expected, roster, topology=args.topology, server_sha=sha)
    except RosterNotPublished:
        message = f'blocked: roster not published at pinned sha {sha}'
        print(message, file=sys.stderr)
        print(json.dumps({'status': 'blocked', 'serverSha': sha, 'topology': args.topology,
                          'missing': [], 'extra': [], 'durableMismatch': [], 'retiredSeen': [],
                          'reasons': [message]}, indent=2))
        return EXIT['blocked']
    except UsageError as exc:
        print(f'error: {exc}', file=sys.stderr)
        print(json.dumps({'status': 'error', 'serverSha': sha, 'reasons': [str(exc)]}, indent=2))
        return EXIT['error']

    for reason in summary['reasons']:
        print(f"{summary['status']}: {reason}", file=sys.stderr)
    if summary['status'] == 'pass':
        print(f'pass: expected-tools.json matches the honua-server roster at {sha}', file=sys.stderr)
    print(json.dumps(summary, indent=2))
    return EXIT[summary['status']]


if __name__ == '__main__':
    sys.exit(main())
