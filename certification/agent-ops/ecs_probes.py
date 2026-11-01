"""Customer probes against a cert cell. Loopback probes stay in live_probes."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from live_probes import assess_traffic

_HOST = re.compile(r"\A[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.cert\.demo\.honua\.io\Z")


def require_public_cell(url: str) -> str:
    parsed = urlsplit(url if isinstance(url, str) else "")
    host = parsed.hostname or ""
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
            or parsed.fragment or parsed.port not in (None, 443) or parsed.path not in ("", "/")
            or not _HOST.fullmatch(host)):
        raise ValueError("public cell url")
    return "https://" + host


def score_public_probes(base_url: str, record: dict, readback: dict) -> dict:
    """RTO is the probe gap bound. RPO is zero only when every acknowledged write is read back."""
    origin = require_public_cell(base_url)
    if not isinstance(readback, dict) or require_public_cell(str(readback.get("base_url"))) != origin:
        raise ValueError("readback origin")
    scored = assess_traffic(record, readback)
    return {
        "rto_seconds": scored["downtime_upper_bound_s"],
        "rpo_zero": scored["rpo_zero"] is True,
        "lost_writes": list(scored["lost_writes"]),
        "committed_writes": scored["committed_writes"],
    }
