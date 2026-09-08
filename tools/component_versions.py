"""Validate explicitly declared contract/schema maps without inventing versions."""
import re

FLOATING_TAGS = {"latest", "nightly", "nightly-aot", "edge", "dev", "main", "master", "trunk", "stable"}
FLOATING_VERSION_RE = re.compile(
    r"(?:^|[^a-z])(?:" + "|".join(re.escape(tag) for tag in sorted(FLOATING_TAGS | {"head"}))
    + r")(?:$|[^a-z])", re.I
)


def version_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("must be a non-empty mapping of names to exact version strings")
    for name, version in value.items():
        if not isinstance(name, str) or not name.strip() or name != name.strip():
            raise ValueError("version names must be non-empty strings without surrounding whitespace")
        if not isinstance(version, str) or not version.strip() or version != version.strip():
            raise ValueError("versions must be non-empty exact strings")
        if re.search(r"tbd|todo|unknown|unresolved|pending", version, re.I) or FLOATING_VERSION_RE.search(
            version
        ) or any(char in version for char in "*<>=~^"):
            raise ValueError("placeholder, floating version or range is not an exact version")
    return dict(value)
