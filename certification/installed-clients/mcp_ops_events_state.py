"""Independent read-only observations for the owned Operate fixture.

These are original database rows, not an expected MCP response. This module
does not seed data, change feature policy, or award operation qualification.
The scenario must separately retain the authenticated fixture setup and the
actual installed client's wire observations.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_ingest_publish_state import database_query
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate

PROFILE = "canonical-mcp-ops-v1"
SCHEMA = "honua.mcp-ops-events-observation/v1"
ROW_FIELDS = frozenset(("eventId", "dedupeKey", "ruleId", "ruleName", "zoneId", "serviceId", "layerId",
    "objectId", "triggerCode", "severity", "generation", "occurredAt", "incidentCode", "incidentDurationMs",
    "lifecycleCode", "acknowledgedAt", "acknowledgedBy", "suppressedUntil", "resolvedAt", "resolvedBy"))


def instant(value):
    if not isinstance(value, str):
        raise ValueError("owned fixture timestamp is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("owned fixture timestamp is malformed") from error
    if parsed.utcoffset() is None:
        raise ValueError("owned fixture timestamp lacks a timezone")
    return parsed

# A single read-only transaction/statement binds both fixtures, their real
# rule and lifecycle rows, clock, and cluster identity to one MVCC snapshot.
# Deliberately do not select encrypted connections, credentials or arbitrary
# event payloads. The exact profile is release-owned, never interpolated input.
STORE_SQL = """
SET TRANSACTION READ ONLY;
SELECT jsonb_build_object(
    'profile', 'canonical-mcp-ops-v1',
    'observedAt', clock_timestamp(),
    'databaseIdentity', jsonb_build_object(
        'clusterId', (SELECT system_identifier::text FROM pg_control_system()),
        'database', current_database(), 'user', current_user),
    'windowEventIds', COALESCE((
        SELECT jsonb_agg(candidate.event_id ORDER BY candidate.event_id)
        FROM (
            SELECT e.event_id FROM honua.alert_events e
            WHERE e.occurred_at BETWEEN
                (SELECT min(occurred_at) FROM honua.alert_events
                 WHERE dedupe_key IN ('canonical-mcp-ops-v1:alert-open', 'canonical-mcp-ops-v1:alert-resolved'))
                AND
                (SELECT max(occurred_at) FROM honua.alert_events
                 WHERE dedupe_key IN ('canonical-mcp-ops-v1:alert-open', 'canonical-mcp-ops-v1:alert-resolved'))
            ORDER BY e.event_id LIMIT 3
        ) candidate
    ), '[]'::jsonb),
    'alerts', COALESCE((
        SELECT jsonb_agg(row_to_json(observation) ORDER BY observation."eventId")
        FROM (
            SELECT e.event_id AS "eventId", e.dedupe_key AS "dedupeKey",
                e.rule_id AS "ruleId", r.rule_name AS "ruleName",
                e.zone_id AS "zoneId", e.service_id AS "serviceId",
                e.layer_id AS "layerId", e.objectid AS "objectId",
                e.trigger_type AS "triggerCode", e.severity,
                e.generation, e.occurred_at AS "occurredAt",
                e.incident_status AS "incidentCode",
                e.incident_duration_ms AS "incidentDurationMs",
                COALESCE(l.lifecycle_status, 0) AS "lifecycleCode",
                l.acknowledged_at AS "acknowledgedAt",
                l.acknowledged_by AS "acknowledgedBy",
                l.suppressed_until AS "suppressedUntil",
                l.resolved_at AS "resolvedAt", l.resolved_by AS "resolvedBy"
            FROM honua.alert_events e
            LEFT JOIN honua.alert_rules r ON r.rule_id = e.rule_id
            LEFT JOIN honua.alert_event_lifecycle l ON l.event_id = e.event_id
            WHERE e.dedupe_key IN (
                'canonical-mcp-ops-v1:alert-open',
                'canonical-mcp-ops-v1:alert-resolved')
        ) observation
    ), '[]'::jsonb)
)::text;
"""


def setup_seed(value):
    """Validate the complete fixed SQL projection before any raw retention.

    The public canonical database password equals its username, ``honua``.
    Checking that substring in safe schema/image names would reject the owned
    composition. These exact shapes cannot carry an arbitrary credential echo.
    """
    fields = {"profile", "sourceHydrated", "setupMethod", "seededAt", "serviceId", "layerId", "actor", "alerts",
        "evaluationExercised", "deliveryExercised", "lifecycleApiExercised"}
    alert_fields = {"openAlertEventId", "resolvedAlertEventId", "zoneId", "ruleId", "ruleName", "zoneName", "dedupeKeys"}
    if (not isinstance(value, dict) or set(value) != fields or value["profile"] != PROFILE
            or value["sourceHydrated"] is not True or value["setupMethod"] != "owned-sql-read-fixture"
            or value["serviceId"] != "canonical-mcp-ops" or type(value["layerId"]) is not int or value["layerId"] != 1
            or value["actor"] != "canonical-mcp-fixture"
            or any(value[key] is not False for key in ("evaluationExercised", "deliveryExercised", "lifecycleApiExercised"))
            or not isinstance(value["alerts"], dict) or set(value["alerts"]) != alert_fields):
        raise ValueError("original SQL setup projection is not the exact owned fixture shape")
    instant(value["seededAt"])
    alerts = value["alerts"]
    if (any(type(alerts[key]) is not int or not 0 < alerts[key] <= 9223372036854775807
            for key in ("openAlertEventId", "resolvedAlertEventId", "zoneId", "ruleId"))
            or alerts["openAlertEventId"] == alerts["resolvedAlertEventId"]
            or alerts["ruleName"] != "Canonical MCP Harbor Entry" or alerts["zoneName"] != "Canonical MCP Harbor"
            or alerts["dedupeKeys"] != [PROFILE + ":alert-open", PROFILE + ":alert-resolved"]):
        raise ValueError("original SQL setup projection changed its fixed values or allocated identities")
    return value


def fixture_rows(value, seed):
    """Authenticate the two independent rows against retained setup identity.

    The full semantic oracle must still check output fields, ordering, filters,
    evidence posture and the original raw invocation for each operation.
    """
    if (not isinstance(seed, dict) or seed.get("profile") != PROFILE
            or seed.get("sourceHydrated") is not True
            or not isinstance(seed.get("alerts"), dict)):
        raise ValueError("original enabled Operate fixture setup is missing")
    selected = seed["alerts"]
    names = ("openAlertEventId", "resolvedAlertEventId", "zoneId", "ruleId")
    if any(type(selected.get(key)) is not int or selected[key] <= 0 for key in names):
        raise ValueError("alert fixture was skipped or lacks allocated store identities")
    if selected["openAlertEventId"] == selected["resolvedAlertEventId"]:
        raise ValueError("distinct fixture events share an identity")
    if (seed.get("serviceId") != "canonical-mcp-ops" or type(seed.get("layerId")) is not int
            or seed["layerId"] != 1 or seed.get("actor") != "canonical-mcp-fixture"
            or selected.get("ruleName") != "Canonical MCP Harbor Entry"
            or selected.get("zoneName") != "Canonical MCP Harbor"
            or selected.get("dedupeKeys") != [PROFILE + ":alert-open", PROFILE + ":alert-resolved"]):
        raise ValueError("setup is not the original owned Operate profile")
    seeded_at = instant(seed.get("seededAt"))
    if (not isinstance(value, dict) or set(value) != {"profile", "observedAt", "databaseIdentity", "alerts", "windowEventIds"}
            or value["profile"] != PROFILE or not isinstance(value["alerts"], list)):
        raise ValueError("independent fixture observation is malformed")
    window_ids = value["windowEventIds"]
    if (not isinstance(window_ids, list) or any(type(key) is not int for key in window_ids)
            or window_ids != sorted((selected["openAlertEventId"], selected["resolvedAlertEventId"]))):
        raise ValueError("owned event window contains missing or ambient durable events")
    identity = value["databaseIdentity"]
    if (not isinstance(identity, dict) or set(identity) != {"clusterId", "database", "user"}
            or not isinstance(identity["clusterId"], str) or not identity["clusterId"].isascii()
            or not identity["clusterId"].isdecimal()
            or not 0 < int(identity["clusterId"]) <= 18446744073709551615
            or identity["database"] != "honua_test" or identity["user"] != "honua"):
        raise ValueError("fixture database identity differs from the owned target")
    if instant(value["observedAt"]) < seeded_at:
        raise ValueError("independent observation predates fixture setup")
    alerts = value["alerts"]
    if len(alerts) != 2 or any(not isinstance(row, dict) for row in alerts):
        raise ValueError("both independent durable alert fixture rows are required")
    by_key = {row.get("dedupeKey"): row for row in alerts}
    expected = {PROFILE + ":alert-open": "openAlertEventId", PROFILE + ":alert-resolved": "resolvedAlertEventId"}
    if set(by_key) != set(expected):
        raise ValueError("fixture row identity is missing, duplicated or substituted")
    for key, field in expected.items():
        row = by_key[key]
        if set(row) != ROW_FIELDS:
            raise ValueError("independent durable alert shape differs")
        for column, expected_value in (("eventId", selected[field]), ("ruleId", selected["ruleId"]),
                                       ("zoneId", selected["zoneId"]), ("layerId", seed.get("layerId"))):
            if type(row.get(column)) is not int or row[column] != expected_value:
                raise ValueError("durable fixture numeric identity differs from allocated setup")
        if (row.get("serviceId") != seed.get("serviceId") or row.get("ruleName") != selected.get("ruleName")
                or not isinstance(row.get("serviceId"), str) or not row["serviceId"]
                or not isinstance(row.get("ruleName"), str) or not row["ruleName"]):
            raise ValueError("durable fixture service/rule identity differs from allocated setup")
        resolved = field == "resolvedAlertEventId"
        expected_values = {"objectId": 4202 if resolved else 4201,
            "generation": 1209002 if resolved else 1209001, "triggerCode": 1,
            "severity": "critical" if resolved else "warning", "incidentCode": 3 if resolved else 1,
            "incidentDurationMs": 420000 if resolved else 0, "lifecycleCode": 3 if resolved else 0}
        for column, expected_value in expected_values.items():
            if type(row[column]) is not type(expected_value) or row[column] != expected_value:
                raise ValueError("independent alert differs from the original causal fixture")
        # Npgsql/Postgres round DateTimeOffset ticks to microseconds; Python's
        # parser truncates the seventh fractional digit. Retain that sole bound.
        occurred = seeded_at - timedelta(minutes=18 if resolved else 9)
        if abs(instant(row["occurredAt"]) - occurred) > timedelta(microseconds=2):
            raise ValueError("alert occurrence differs from original setup; do not re-seed")
        if any(row[column] is not None for column in ("acknowledgedAt", "acknowledgedBy", "suppressedUntil")):
            raise ValueError("owned alert acquired an unrequested lifecycle transition")
        if resolved:
            if (row["resolvedBy"] != seed["actor"]
                    or abs(instant(row["resolvedAt"]) - (seeded_at - timedelta(minutes=7))) > timedelta(microseconds=2)):
                raise ValueError("durable resolution differs from the original lifecycle mutation")
        elif row["resolvedAt"] is not None or row["resolvedBy"] is not None:
            raise ValueError("owned open event was unexpectedly resolved")
    return by_key


def observe(seed, *, password, candidate_source, candidate_image, sql=database_query, inspect=inspect_candidate,
            clock=lambda: datetime.now(timezone.utc)):
    started = clock().isoformat()
    before = inspect(candidate_source, candidate_image)
    value = strict_json(sql(STORE_SQL, password))
    fixture_rows(value, seed)
    after = inspect(candidate_source, candidate_image)
    if before != after:
        raise ValueError("candidate runtime changed during independent store observation")
    completed = clock().isoformat()
    if not instant(started) <= instant(value["observedAt"]) <= instant(completed):
        raise ValueError("database observation lies outside its actual probe bracket")
    return {"schema": SCHEMA, "startedAt": started,
            "completedAt": completed,
            "runtimeBinding": before, "store": value}


PREFLIGHT_URL = "http://127.0.0.1:8080/api/v1/admin/deploy/preflight?includeDiagnostics=true"


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, stream, code, message, headers, destination):
        raise ValueError("owned preflight must never forward its managed credential")


def require_preflight_identity(credential, salt):
    if (not isinstance(credential, str) or not credential or len(credential) > 8192
            or any(character in credential for character in "\r\n")
            or not isinstance(salt, str) or not re.fullmatch(r"[a-f0-9]{32}", salt)):
        raise ValueError("normal preflight requires private managed credential and shared observation salt")


def preflight(credential, salt, *, clock=lambda: datetime.now(timezone.utc), opener=None):
    require_preflight_identity(credential, salt)
    opener = opener or build_opener(ProxyHandler({}), RefuseRedirect())
    started = clock().isoformat()
    request = Request(PREFLIGHT_URL, headers={"X-API-Key": credential, "Accept": "application/json"}, method="GET")
    with opener.open(request, timeout=20) as response:
        if response.status != 200 or response.headers.get_content_type() != "application/json":
            raise ValueError("normal authenticated deploy preflight did not return JSON success")
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("normal deploy preflight exceeded its bounded original response")
    text = raw.decode("utf-8")
    if not isinstance(strict_json(text), dict):
        raise ValueError("normal deploy preflight did not return its root object")
    from mcp_ops_events_setup import safe_observation
    safe_observation(strict_json(text), credential)
    fingerprint = hashlib.scrypt(credential.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()
    return {"method": "GET", "url": PREFLIGHT_URL, "status": 200, "responseText": text,
        "responseSha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
        "credentialFingerprint": "scrypt:" + salt + ":" + fingerprint,
        "startedAt": started, "completedAt": clock().isoformat()}


def observe_context(seed, declaration, *, password, credential, credential_salt, candidate_source, candidate_image,
                    sql=database_query, inspect=inspect_candidate, clock=lambda: datetime.now(timezone.utc),
                    read_configuration=None, read_preflight=None):
    from mcp_ops_events_config import observe_configuration
    from mcp_ops_events_setup import safe_observation
    if not isinstance(password, str) or not password:
        raise ValueError("owned context requires its private database credential")
    require_preflight_identity(credential, credential_salt)
    started = clock().isoformat()
    before = inspect(candidate_source, candidate_image)
    configuration = (read_configuration or observe_configuration)(declaration, before)
    original_preflight = (read_preflight or preflight)(credential, credential_salt)
    original_store = observe(seed, password=password, candidate_source=candidate_source, candidate_image=candidate_image,
        sql=sql, inspect=inspect, clock=clock)
    after = inspect(candidate_source, candidate_image)
    if before != after or before != original_store["runtimeBinding"]:
        raise ValueError("candidate runtime changed during independent operate context observation")
    result = {"schema": "honua.mcp-ops-events-context/v1", "startedAt": started,
        "completedAt": clock().isoformat(), "storeObservation": original_store,
        "runtimeBinding": before, "configuration": configuration, "preflight": original_preflight}
    safe_observation(result, credential)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--seed-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--configuration", type=Path)
    parser.add_argument("--credential-salt")
    parser.add_argument("--restart", action="store_true")
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("independent observation output already exists")
    seed = strict_json(args.seed_report.read_bytes())
    password = os.environ.get("HONUA_CERT_DB_PASSWORD")
    if not password:
        raise ValueError("owned disposable database credential is missing")
    if (args.configuration is None) != (args.credential_salt is None):
        raise ValueError("owned context requires both original configuration and shared credential salt")
    declaration = strict_json(args.configuration.read_bytes()) if args.configuration else None
    if args.configuration:
        require_preflight_identity(os.environ.get("HONUA_CERT_ADMIN_API_KEY"), args.credential_salt)
    if args.restart:
        from mcp_style_state import restart
        original = inspect_candidate(args.candidate_source_sha, args.candidate_image)
        restart({"candidateSourceSha": args.candidate_source_sha, "candidateImage": args.candidate_image}, original)
    result = observe_context(seed, declaration, password=password,
        credential=os.environ.get("HONUA_CERT_ADMIN_API_KEY"), credential_salt=args.credential_salt,
        candidate_source=args.candidate_source_sha, candidate_image=args.candidate_image) if args.configuration else \
        observe(seed, password=password, candidate_source=args.candidate_source_sha, candidate_image=args.candidate_image)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as destination:
        json.dump(result, destination, indent=2, allow_nan=False)
        destination.write("\n")


if __name__ == "__main__":
    main()
