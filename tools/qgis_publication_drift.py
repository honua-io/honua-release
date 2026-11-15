"""Drift view for an unpublished QGIS plugin.

A rate-limited GitHub API probe records extra fields beside a public-page 404.
Those fields are the probe, not a publication. Drift comparison accepts only
that exact row and the blocker text it produces.
"""
from __future__ import annotations

HTTP_FORBIDDEN = 403
HTTP_NOT_FOUND = 404
HTTP_TOO_MANY_REQUESTS = 429

QGIS_REPO_URL = "https://api.github.com/repos/honua-io/honua-qgis-plugin"
QGIS_PUBLIC_REPO_URL = "https://github.com/honua-io/honua-qgis-plugin"
# plugins.qgis.org keys a plugin by its ZIP's top-level package folder, not its display name.
# honua-qgis-plugin scripts/package.py builds honua_gis_assistant-<version>.zip with the single
# top-level folder honua_gis_assistant/, so that is the listing slug.
QGIS_PLUGIN_URL = "https://plugins.qgis.org/plugins/honua_gis_assistant/"
QGIS_BOUNDARY = (
    "https://api.github.com/repos/honua-io/honua-qgis-plugin returned 404 to an unauthenticated request "
    "(a private repository is not distinguishable from a missing one) and "
    "https://plugins.qgis.org/plugins/honua_gis_assistant/ returned 404. honua-qgis-plugin CI builds a ZIP artifact only; "
    "it does not submit to the QGIS plugin repository. Publication still needs an operator visibility, "
    "history, and signing review, an exact release ZIP, and OSGeo submission (honua-qgis-plugin#29). "
    "No signing secret is recorded here."
)
_QGIS_CHANNEL_ID = "qgis:honua-qgis-plugin"
_QGIS_PUBLICATION = "QGIS plugin honua_gis_assistant"
_QGIS_DENIAL_KEYS = frozenset({
    "api_http_status",
    "canonical_url",
    "disposition",
    "http_status",
    "id",
    "observed_http_status",
    "observed_url",
    "plugin_http_status",
    "publication",
    "urls",
})


def boundary(channel: dict) -> str:
    """Blocker text for an unpublished QGIS plugin, including a rate-limit observation."""
    if channel.get("observed_url"):
        return (
            f"{channel['canonical_url']} returned {channel['api_http_status']} to an unauthenticated request "
            "(not evidence the repository is private). "
            f"{channel['observed_url']} returned {channel['observed_http_status']}. "
            f"{QGIS_PLUGIN_URL} returned {channel['plugin_http_status']}. "
            "honua-qgis-plugin CI builds a ZIP artifact only; "
            "it does not submit to the QGIS plugin repository. Publication still needs an operator visibility, "
            "history, and signing review, an exact release ZIP, and OSGeo submission (honua-qgis-plugin#29). "
            "No signing secret is recorded here."
        )
    return QGIS_BOUNDARY


def rate_limit_absence(channel: dict) -> bool:
    """True only for the exact rate-limit row that still shows the plugin unpublished."""
    if set(channel) != _QGIS_DENIAL_KEYS:
        return False
    return (
        channel.get("id") == _QGIS_CHANNEL_ID
        and channel.get("disposition") == "blocked-on-operator"
        and channel.get("publication") == _QGIS_PUBLICATION
        and channel.get("plugin_http_status") == HTTP_NOT_FOUND
        and channel.get("urls") == [QGIS_REPO_URL, QGIS_PLUGIN_URL]
        and channel.get("canonical_url") == QGIS_REPO_URL
        and channel.get("observed_url") == QGIS_PUBLIC_REPO_URL
        and channel.get("observed_http_status") == HTTP_NOT_FOUND
        and channel.get("api_http_status") in {HTTP_FORBIDDEN, HTTP_TOO_MANY_REQUESTS}
        and channel.get("http_status") == channel.get("api_http_status")
    )


def absent_channel() -> dict:
    return {
        "disposition": "blocked-on-operator",
        "http_status": HTTP_NOT_FOUND,
        "id": _QGIS_CHANNEL_ID,
        "plugin_http_status": HTTP_NOT_FOUND,
        "publication": _QGIS_PUBLICATION,
        "urls": [QGIS_REPO_URL, QGIS_PLUGIN_URL],
    }


def project_absence(channels: list, blockers: list) -> tuple[list, list]:
    """Return channels and blockers with one exact rate-limit absence folded to the 404 row."""
    projected_channels = []
    denial = None
    for channel in channels:
        if rate_limit_absence(channel):
            denial = channel
            projected_channels.append(absent_channel())
        else:
            projected_channels.append(channel)
    if denial is None:
        return projected_channels, blockers
    expected = boundary(denial)
    projected_blockers = []
    for blocker in blockers:
        if (
            blocker.get("kind") == "blocked-on-operator"
            and blocker.get("channels") == [_QGIS_CHANNEL_ID]
            and blocker.get("publication") == _QGIS_PUBLICATION
            and blocker.get("boundary") == expected
        ):
            blocker = {**blocker, "boundary": QGIS_BOUNDARY}
        projected_blockers.append(blocker)
    return projected_channels, projected_blockers
