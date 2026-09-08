"""Validate explicitly declared contract/schema maps without inventing versions."""
import re


def version_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ValueError("must be a non-empty mapping of names to exact version strings")
    for name, version in value.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError("version names must be non-empty strings")
        if not isinstance(version, str) or not version.strip() or version != version.strip():
            raise ValueError("versions must be non-empty exact strings")
        if re.search(r"tbd|todo|unknown|unresolved|pending", version, re.I) or re.search(
            r"(?:^|[^a-z])(latest|nightly|main|trunk|head)(?:$|[^a-z])", version, re.I
        ) or any(char in version for char in "*<>=~^"):
            raise ValueError("placeholder, floating version or range is not an exact version")
    return dict(value)
