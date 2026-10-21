"""Normal read:* positive before the separately judged Ops permission denial.

This is a supporting product data read, not another primary MCP operation.
The server checks scoped-key read grants before its public/coarse fallback.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mcp_map_package_validation as wire
from mcp_ops_events_state import RefuseRedirect, instant, require_preflight_identity
from mcp_render_validation import inspect_candidate

SCHEMA = "honua.mcp-ops-events-limited-read/v1"


def data_url(fixture, bootstrap):
    wire.scenario(fixture, bootstrap)
    area = bootstrap["ownedArea"]
    query = urlencode({"f": "json", "where": "1=1", "returnCountOnly": "true"})
    return ("http://127.0.0.1:8080/rest/services/" + quote(area["serviceId"], safe="")
        + "/FeatureServer/" + str(area["layerId"]) + "/query?" + query)


def verify(value, fixture, bootstrap, identity, runtime):
    fields = {"schema", "method", "url", "status", "responseText", "responseSha256", "credentialFingerprint",
        "runtimeBefore", "runtimeAfter", "startedAt", "completedAt"}
    if (not isinstance(value, dict) or set(value) != fields or value["schema"] != SCHEMA
            or value["method"] != "GET" or value["url"] != data_url(fixture, bootstrap)
            or type(value["status"]) is not int or value["status"] != 200
            or value["credentialFingerprint"] != identity["credentialFingerprint"]
            or not wire.same_json(value["runtimeBefore"], runtime) or not wire.same_json(value["runtimeAfter"], runtime)
            or not instant(runtime["startedAt"]) <= instant(value["startedAt"]) <= instant(value["completedAt"])
            or not isinstance(value["responseText"], str) or len(value["responseText"].encode()) > 65536
            or value["responseSha256"] != "sha256:" + hashlib.sha256(value["responseText"].encode()).hexdigest()):
        raise ValueError("normal limited data read lost its original request, response, key, or runtime binding")
    rebound = dict(bootstrap); rebound["runtimeBinding"] = runtime
    wire.scenario(fixture, rebound)
    body = wire.strict_json(value["responseText"])
    count = len(wire.strict_json(fixture)["area"]["features"])
    if (not isinstance(body, dict) or set(body) != {"count", "exceededTransferLimit"} or type(body["count"]) is not int
            or body["exceededTransferLimit"] is not False or count <= 0 or body["count"] != count):
        raise ValueError("normal limited reader did not positively read the original nonempty owned data")
    return body["count"]


def read(fixture, bootstrap, *, credential, credential_salt, inspect=inspect_candidate,
         clock=lambda: datetime.now(timezone.utc), opener=None):
    require_preflight_identity(credential, credential_salt)
    url = data_url(fixture, bootstrap)
    source, image = bootstrap["candidateSourceSha"], bootstrap["candidateImage"]
    started = clock().isoformat()
    before = inspect(source, image)
    opener = opener or build_opener(ProxyHandler({}), RefuseRedirect())
    request = Request(url, method="GET", headers={"X-API-Key": credential, "Accept": "application/json"})
    with opener.open(request, timeout=20) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("normal limited reader did not receive JSON data-read success")
        raw = response.read(65537)
    if len(raw) > 65536:
        raise ValueError("normal limited data read exceeded its bounded original response")
    text = raw.decode("utf-8")
    after = inspect(source, image)
    fingerprint = "scrypt:" + credential_salt + ":" + hashlib.scrypt(credential.encode(),
        salt=bytes.fromhex(credential_salt), n=16384, r=8, p=1, dklen=32).hex()
    result = {"schema": SCHEMA, "method": "GET", "url": url, "status": 200,
        "responseText": text, "responseSha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "credentialFingerprint": fingerprint, "runtimeBefore": before, "runtimeAfter": after,
        "startedAt": started, "completedAt": clock().isoformat()}
    # Validate the original response's exact count-only shape before retention.
    # Unknown response fields cannot carry a credential echo into a receipt.
    verify(result, fixture, bootstrap, {"credentialFingerprint": fingerprint}, before)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True)
    parser.add_argument("--credential-salt", required=True)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    try:
        fixture, bootstrap = args.fixture.read_bytes(), wire.strict_json(args.bootstrap.read_bytes())
        if (bootstrap.get("candidateSourceSha") != args.candidate_source_sha
                or bootstrap.get("candidateImage") != args.candidate_image):
            raise ValueError("normal read fixture selected another candidate")
        value = read(fixture, bootstrap, credential=os.environ.get("HONUA_MCP_READ_API_KEY"), credential_salt=args.credential_salt)
        print(json.dumps(value, allow_nan=False)); return 0
    except Exception:
        print("normal owned limited data read refused", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
