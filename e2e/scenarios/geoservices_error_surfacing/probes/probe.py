#!/usr/bin/env python3
"""GeoServices error-surfacing probe — Python SDK (honua-sdk).

Forces the server to return HTTP 200 with a GeoServices `{"error": {...}}` body and asserts the SDK
RAISES rather than returning a success object. This is the regression test for sdk-python#122.

Exit-code contract (shared by every language probe):
  0 = PASS  (SDK raised, as it must)
  1 = FAIL  (SDK returned success on a 200+{error} — the bug)
  2 = SKIP  (SDK not installed)
"""
import os
import sys

SERVER = os.environ["HONUA_SERVER_URL"].rstrip("/")

try:
    # TODO(#7): confirm the real import surface once honua-sdk-python is published & version-pinned.
    import honua_sdk  # type: ignore
except Exception as e:  # noqa: BLE001 - any import failure means "can't run this probe"
    print(f"SKIP: honua-sdk (python) not importable: {e}")
    sys.exit(2)


def force_200_error():
    """Issue a GeoServices request the server answers with 200 + {error}.

    GeoServices/ArcGIS convention: errors are delivered in-band as
    `{"error": {"code": ..., "message": ..., "details": [...]}}` with an HTTP 200 status line — which
    is exactly why naive clients (sdk-python#122) treated it as success.

    TODO(#7): pin the exact endpoint + params that deterministically yield a 200+{error} against the
    composed server (e.g. a query with a malformed `where`, or a non-existent layer id).
    """
    client = honua_sdk.Client(base_url=SERVER)  # TODO(#7): confirm constructor
    return client.geoservices.query(
        layer=0,
        where="1=1)) DROP",          # malformed predicate -> server returns 200+{error}
        out_fields="*",
    )


try:
    result = force_200_error()
except Exception as e:  # noqa: BLE001 - any raise is the CORRECT behaviour here
    print(f"PASS: python SDK raised on 200+{{error}}: {type(e).__name__}: {e}")
    sys.exit(0)

print(f"FAIL: python SDK returned success on 200+{{error}}: {result!r}")
sys.exit(1)
