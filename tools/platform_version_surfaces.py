#!/usr/bin/env python3
"""Compare the platform version every customer-visible surface reports for one candidate.

The server version endpoints, the OCI `org.opencontainers.image.version`
label, the Console/Studio `version.json`, the CLI and the docs must name the same platform
version as the candidate's signed lock. Component versions keep their own policy (ruling R22):
Console, the CLIs and the docs therefore report the platform version in a separate
`platformVersion` field, never by renaming their own package version.

The observations file is JSON collected from the installed candidate:

    {"sourceRevision": "<40 hex>",
     "server": {"capabilities": "<serverVersion>", "admin": "<version>"},
     "oci": "<image label>",
     "console": {<version.json>}, "cli": {<cli version --json>}, "docs": {<docs version.json>}}

The server may report `<version>+<sha7>` for the candidate's own revision. A surface that is
missing, reports no platform version or reports another one fails the comparison.

`collect` builds that file from an installed candidate: the anonymous capabilities endpoint,
the admin version endpoint (API key from HONUA_API_KEY, never written out), `docker image
inspect` of the candidate reference, the Console `/version.json`, a CLI's JSON version output
and the published docs `version.json`. Redirects are refused, only the name, version, commit and
platformVersion fields of a surface document are kept, and an unreachable surface is absent.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

import no_redirect
from compose_upgrade_version import runtime_platform_version
from platform_version import artifact_version

SCHEMA = "honua.platform-version-surfaces/v1"
SERVER_ENDPOINTS = ("capabilities", "admin")
FIELD_SURFACES = ("console", "cli", "docs")


def _server(observed: dict, revision: str) -> dict:
    server = observed.get("server")
    if not isinstance(server, dict):
        return {endpoint: None for endpoint in SERVER_ENDPOINTS}
    return {endpoint: runtime_platform_version(server.get(endpoint), revision) for endpoint in SERVER_ENDPOINTS}


def _field(observed: dict, surface: str):
    document = observed.get(surface)
    if not isinstance(document, dict):
        return None
    value = document.get("platformVersion")
    return value if isinstance(value, str) else None


def reported_versions(observed: dict) -> dict:
    """Map every required surface to the platform version it reports, or None."""
    revision = str(observed.get("sourceRevision") or "")
    reported = {f"server.{endpoint}": value for endpoint, value in _server(observed, revision).items()}
    oci = observed.get("oci")
    reported["oci"] = oci if isinstance(oci, str) else None
    reported.update({surface: _field(observed, surface) for surface in FIELD_SURFACES})
    return reported


def compare(label: str, observed: dict) -> dict:
    """Return the receipt; every surface must report the lock label's artifact version."""
    try:
        expected = artifact_version(label)
    except ValueError as error:
        return {"schema": SCHEMA, "status": "fail", "why": str(error), "surfaces": {}}
    reported = reported_versions(observed if isinstance(observed, dict) else {})
    surfaces = {name: {"reported": value, "match": value == expected} for name, value in reported.items()}
    mismatched = sorted(name for name, row in surfaces.items() if not row["match"])
    if mismatched:
        why = f"surfaces do not report {expected}: {', '.join(mismatched)}"
        return {"schema": SCHEMA, "status": "fail", "expectedVersion": expected, "why": why, "surfaces": surfaces}
    why = f"every surface reports {expected}"
    return {"schema": SCHEMA, "status": "pass", "expectedVersion": expected, "why": why, "surfaces": surfaces}


RETAINED_FIELDS = ("name", "version", "commit", "platformVersion")


_OPENER = no_redirect.opener()  # an API key never follows a response to another origin


def fetch_json(url: str, headers: dict | None = None, opener=_OPENER):
    """GET a JSON document without following redirects; any failure is an absent surface."""
    request = urllib.request.Request(url, headers={"Accept": "application/json", **(headers or {})})
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None


def _retained(document):
    """Keep only the identity fields of a surface document; nothing else is written out."""
    if not isinstance(document, dict):
        return None
    return {key: document[key] for key in RETAINED_FIELDS if key in document}


def _data(document, key: str):
    data = document.get("data") if isinstance(document, dict) else None
    return data.get(key) if isinstance(data, dict) else None


def oci_label(image: str, run=subprocess.run):
    """The candidate image's OCI version label, or None when docker cannot inspect it."""
    template = '{{index .Config.Labels "org.opencontainers.image.version"}}'
    result = run(["docker", "image", "inspect", "--format", template, image],
                 capture_output=True, text=True, check=False)
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def collect(args, fetch=fetch_json, label=oci_label) -> dict:
    """Observe every surface of one installed candidate."""
    server = args.server.rstrip("/")
    key = os.environ.get("HONUA_API_KEY", "")
    admin = fetch(f"{server}/api/v1/admin/version", {"X-API-Key": key}) if key else None
    observed = {"sourceRevision": args.source_revision,
                "server": {"capabilities": _data(fetch(f"{server}/api/v1/admin/capabilities"), "serverVersion"),
                           "admin": _data(admin, "version")},
                "oci": label(args.image)}
    if args.console:
        observed["console"] = _retained(fetch(args.console.rstrip("/") + "/version.json"))
    if args.cli_json:
        observed["cli"] = _retained(_read_json(args.cli_json))
    if args.docs:
        observed["docs"] = _retained(fetch(args.docs))
    return observed


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(document: dict, out: Path | None) -> None:
    text = json.dumps(document, indent=2, sort_keys=True) + "\n"
    if out:
        out.write_text(text, encoding="utf-8")
    sys.stdout.write(text)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("compare", help="compare collected observations with the lock label")
    check.add_argument("--label", required=True, help="the candidate lock's platform label")
    check.add_argument("--observed", type=Path, required=True, help="collected surface observations")
    check.add_argument("--out", type=Path, help="write the receipt here as well as stdout")
    gather = commands.add_parser("collect", help="observe the surfaces of an installed candidate")
    gather.add_argument("--server", required=True, help="server base URL")
    gather.add_argument("--image", required=True, help="candidate server image reference (by digest)")
    gather.add_argument("--source-revision", required=True, help="candidate honua-server source revision")
    gather.add_argument("--console", help="Console base URL")
    gather.add_argument("--cli-json", type=Path, help="file holding a CLI's JSON version output")
    gather.add_argument("--docs", help="URL of the published docs version.json")
    gather.add_argument("--out", type=Path, help="write the observations here as well as stdout")
    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "collect":
        _write(collect(args), args.out)
        return 0
    receipt = compare(args.label, _read_json(args.observed))
    _write(receipt, args.out)
    return 0 if receipt["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
