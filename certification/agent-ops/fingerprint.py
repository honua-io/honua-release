"""Identity of a governed service document. The inputs are the four governed fields."""

from __future__ import annotations

import hashlib
import json


def governed_fingerprint(style, fields, filter_value, publication) -> str:
    blob = json.dumps(
        {
            "style": style,
            "fields": fields,
            "filter": filter_value,
            "publication": publication,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
