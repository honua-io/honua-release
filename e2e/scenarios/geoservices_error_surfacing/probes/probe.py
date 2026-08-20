#!/usr/bin/env python3
"""Require the frozen Python SDK to surface a real GeoServices error envelope."""
import os
import sys

SERVER = os.environ["HONUA_SERVER_URL"].rstrip("/")

try:
    from honua_sdk import HonuaClient, HonuaHttpError
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: honua-sdk not importable: {exc}")
    sys.exit(2)

try:
    with HonuaClient(SERVER) as client:
        result = client.query_features("__honua_parity_missing__", 0, where="1=1))")
except HonuaHttpError as exc:
    print(f"PASS: Python SDK raised HonuaHttpError on 200+{{error}}: {exc}")
    sys.exit(0)
except Exception as exc:  # noqa: BLE001
    print(f"FAIL: Python SDK raised the wrong exception: {type(exc).__name__}: {exc}")
    sys.exit(1)

print(f"FAIL: Python SDK returned success on 200+{{error}}: {result!r}")
sys.exit(1)
