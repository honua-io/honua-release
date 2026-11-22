"""Exercise persisted credentials without publishing their secret material."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4


CONNECTION_FIELDS = (
    "connectionId", "name", "host", "port", "databaseName", "username", "provider",
    "sslRequired", "sslMode", "storageType", "isActive",
)
KEY_FIELDS = ("id", "name", "keyPrefix", "permissions", "status", "createdAt")
CHECKS = ("connectionUnchanged", "storedCredentialHealthy", "existingApiKeyAuthenticated", "apiKeyUnchanged")


class ProbeError(ValueError):
    """A sanitized failure that may safely appear in an uploaded receipt."""


class Api:
    def __init__(self, base: str, key: str):
        self.base = base.rstrip("/") + "/api/v1/admin"
        self.key = key

    def request(self, path: str, body=None):
        payload = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=payload, headers={
            "X-API-Key": self.key, "Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=30) as response:
                document = json.load(response)
        except HTTPError as error:
            raise ProbeError(f"admin request rejected (HTTP {error.code})") from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise ProbeError("admin request failed or returned invalid JSON") from None
        return self.unwrap(document)

    def unwrap(self, document):
        if not isinstance(document, dict) or "data" not in document:
            raise ProbeError("admin response has no data envelope")
        return document["data"]


def selected_fields(document: dict, fields: tuple) -> dict:
    if not isinstance(document, dict) or any(field not in document for field in fields):
        raise ProbeError("persisted metadata is missing required fields")
    return {field: document[field] for field in fields}


def identifier(value) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise ProbeError("persisted credential identifier is invalid") from None


def connection(api: Api, connection_id: str) -> dict:
    value = selected_fields(api.request(f"/connections/{connection_id}"), CONNECTION_FIELDS)
    if value["connectionId"] != connection_id or value["isActive"] is not True:
        raise ProbeError("seeded connection is missing or inactive")
    return value


def health(api: Api, connection_id: str):
    value = api.request(f"/connections/{connection_id}/test", {})
    if (not isinstance(value, dict) or value.get("connectionId") != connection_id
            or value.get("isHealthy") is not True):
        raise ProbeError("saved connection credential did not authenticate")


def key_metadata(api: Api, key_id: str) -> dict:
    keys = api.request("/api-keys/")
    if not isinstance(keys, list):
        raise ProbeError("API key listing is invalid")
    matches = [key for key in keys if isinstance(key, dict) and key.get("id") == key_id]
    if len(matches) != 1:
        raise ProbeError("pre-upgrade API key is missing or duplicated")
    return {**selected_fields(matches[0], KEY_FIELDS), "expiresAt": matches[0].get("expiresAt")}


def seed(api: Api, manifest: dict) -> dict:
    connection_id = identifier(manifest.get("connectionId"))
    before = connection(api, connection_id)
    health(api, connection_id)
    minted = api.request("/api-keys/", {
        "name": f"upgrade-preservation-{uuid4().hex}", "permissions": ["admin:read"]})
    if not isinstance(minted, dict) or not isinstance(minted.get("key"), str) or not minted["key"]:
        raise ProbeError("API key creation did not return a key")
    key_id = identifier(minted.get("apiKey", {}).get("id"))
    reader = Api(api.base.removesuffix("/api/v1/admin"), minted["key"])
    if connection(reader, connection_id) != before:
        raise ProbeError("new API key could not read the seeded connection")
    return {"connection": before, "key": minted["key"], "apiKey": key_metadata(api, key_id)}


def verify(api: Api, state: dict) -> dict:
    connection_id = identifier(state["connection"]["connectionId"])
    key_id = identifier(state["apiKey"]["id"])
    reader = Api(api.base.removesuffix("/api/v1/admin"), state["key"])
    if connection(reader, connection_id) != state["connection"]:
        raise ProbeError("seeded connection metadata changed across the upgrade")
    health(api, connection_id)
    if key_metadata(api, key_id) != state["apiKey"]:
        raise ProbeError("API key metadata changed across the upgrade")
    return {"schema": 1, "status": "pass", "connectionId": connection_id, "apiKeyId": key_id,
            "checks": dict.fromkeys(CHECKS, True)}


def preservation_evidence(pair: dict, observed: dict, contract: dict) -> dict:
    if pair.get("candidate_version") != "2026.1.0":
        return {"status": "not-applicable", "why": "pair is not the GA upgrade"}
    value = observed.get(contract["key"])
    checks = contract["checks"]
    try:
        if (not isinstance(value, dict) or value.get("schema") != 1
                or value.get("status") != "pass"
                or not isinstance(value.get("checks"), dict)
                or any(value["checks"].get(check) is not True for check in checks)):
            raise ProbeError(f"{contract['key']} preservation proof is missing or incomplete")
        return {"status": "pass", **contract["fields"](value), "checks": dict.fromkeys(checks, True)}
    except ProbeError as error:
        return {"status": "fail", "why": str(error)}


def credential_fields(value: dict) -> dict:
    return {field: identifier(value.get(field)) for field in ("connectionId", "apiKeyId")}


def credential_evidence(pair: dict, observed: dict) -> dict:
    return preservation_evidence(pair, observed, {
        "key": "credentials", "checks": CHECKS, "fields": credential_fields})


def write_private(path: Path, state: dict):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(state, stream)


def run_probe(api_type, seed_probe, verify_probe) -> int:
    parser = argparse.ArgumentParser(description="Exercise persisted state across an upgrade")
    parser.add_argument("mode", choices=("seed", "verify"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    result = {"schema": 1, "status": "fail", "why": "preservation probe did not finish"}
    exit_code = 1
    args.receipt.write_text(json.dumps(result) + "\n")
    try:
        api = api_type(os.environ["E2E_BASE"], os.environ["E2E_API_KEY"])
        if args.mode == "seed":
            state = seed_probe(api, json.loads(args.manifest.read_text()))
            write_private(args.state, state)
            result["why"] = "seeded on prior; candidate verification is still required"
        else:
            result = verify_probe(api, json.loads(args.state.read_text()))
        exit_code = 0
    except ProbeError as error:
        result["why"] = str(error)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result["why"] = "preservation probe input or private state is invalid"
    args.receipt.write_text(json.dumps(result) + "\n")
    return exit_code


def main() -> int:
    return run_probe(Api, seed, verify)


if __name__ == "__main__":
    raise SystemExit(main())
