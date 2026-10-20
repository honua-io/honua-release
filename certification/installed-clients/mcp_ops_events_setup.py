"""Seed two fixture-origin durable events on the owned Production candidate.

This is bounded test data provisioning, like the canonical base-schema seed.
It never enables Preview alerts, changes editions, calls a Development endpoint,
or claims to exercise alert evaluation, notification delivery, or lifecycle APIs.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_ingest_publish_state import database_query
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate
from mcp_ops_events_state import PROFILE, observe, setup_seed

# Fixed release-owned values. This statement cannot be redirected to another
# tenant, database, service, schema, table, or fixture profile by caller input.
# The inherited psql helper wraps everything in one transaction with stop-on-error.
SETUP_SQL = """
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '15s';
DO $$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('honua.certification.canonical-mcp-ops-v1', 0));
    IF EXISTS (SELECT 1 FROM honua.alert_events WHERE dedupe_key LIKE 'canonical-mcp-ops-v1:%')
       OR EXISTS (SELECT 1 FROM honua.alert_zones WHERE service_id = 'canonical-mcp-ops')
       OR EXISTS (SELECT 1 FROM honua.alert_rules WHERE service_id = 'canonical-mcp-ops') THEN
        RAISE EXCEPTION 'Owned MCP read fixture already exists; reuse original setup receipt, never re-seed';
    END IF;
END $$;
WITH fixture_clock AS (SELECT transaction_timestamp() AS seeded_at),
zone AS (
    INSERT INTO honua.alert_zones(service_id, zone_name, geometry, metadata, is_active)
    VALUES ('canonical-mcp-ops', 'Canonical MCP Harbor',
        ST_GeomFromText('MULTIPOLYGON (((-157.9 21.2, -157.8 21.2, -157.8 21.3, -157.9 21.3, -157.9 21.2)))', 4326),
        '{"fixtureProfile":"canonical-mcp-ops-v1","purpose":"canonical-mcp-read-semantics"}'::jsonb, FALSE)
    RETURNING zone_id
), rule AS (
    INSERT INTO honua.alert_rules(service_id, layer_id, zone_id, rule_name, trigger_type,
        conditions, cooldown_seconds, severity, edition_required, channels, is_active)
    SELECT 'canonical-mcp-ops', 1, zone_id, 'Canonical MCP Harbor Entry', 1,
        '{"fixtureProfile":"canonical-mcp-ops-v1","purpose":"canonical-mcp-read-semantics"}'::jsonb,
        60, 'warning', 1, '{}'::text[], FALSE FROM zone
    RETURNING rule_id, zone_id
), inserted_events AS (
    INSERT INTO honua.alert_events(dedupe_key, rule_id, zone_id, service_id, layer_id, objectid,
        trigger_type, generation, severity, occurred_at, payload, incident_status, incident_duration_ms)
    SELECT 'canonical-mcp-ops-v1:' || fixture.suffix, rule.rule_id, rule.zone_id, 'canonical-mcp-ops', 1,
        fixture.object_id, 1, fixture.generation, fixture.severity,
        fixture_clock.seeded_at - fixture.age,
        '{"fixtureProfile":"canonical-mcp-ops-v1","purpose":"canonical-mcp-read-semantics"}'::jsonb,
        fixture.incident_status, fixture.duration_ms
    FROM rule CROSS JOIN fixture_clock CROSS JOIN
        (VALUES ('alert-open', 4201::bigint, 1209001::bigint, 'warning', interval '9 minutes', 1::smallint, 0::bigint),
                ('alert-resolved', 4202::bigint, 1209002::bigint, 'critical', interval '18 minutes', 3::smallint, 420000::bigint))
        fixture(suffix, object_id, generation, severity, age, incident_status, duration_ms)
    RETURNING event_id, dedupe_key
), lifecycle AS (
    INSERT INTO honua.alert_event_lifecycle(event_id, lifecycle_status, resolved_at, resolved_by, note, updated_at)
    SELECT event_id, 3, fixture_clock.seeded_at - interval '7 minutes', 'canonical-mcp-fixture',
        'Fixture-origin resolved state; no lifecycle API coverage.', fixture_clock.seeded_at - interval '7 minutes'
    FROM inserted_events CROSS JOIN fixture_clock WHERE dedupe_key = 'canonical-mcp-ops-v1:alert-resolved'
    RETURNING event_id
)
SELECT jsonb_build_object('profile', 'canonical-mcp-ops-v1', 'sourceHydrated', TRUE,
    'setupMethod', 'owned-sql-read-fixture', 'seededAt', fixture_clock.seeded_at,
    'serviceId', 'canonical-mcp-ops', 'layerId', 1, 'actor', 'canonical-mcp-fixture',
    'alerts', jsonb_build_object('zoneId', rule.zone_id, 'zoneName', 'Canonical MCP Harbor',
        'ruleId', rule.rule_id, 'ruleName', 'Canonical MCP Harbor Entry',
        'openAlertEventId', (SELECT event_id FROM inserted_events WHERE dedupe_key = 'canonical-mcp-ops-v1:alert-open'),
        'resolvedAlertEventId', (SELECT event_id FROM lifecycle),
        'dedupeKeys', jsonb_build_array('canonical-mcp-ops-v1:alert-open', 'canonical-mcp-ops-v1:alert-resolved')),
    'evaluationExercised', FALSE, 'deliveryExercised', FALSE, 'lifecycleApiExercised', FALSE)::text
FROM rule CROSS JOIN fixture_clock;
"""


def safe_observation(value, password):
    pending = [value]
    while pending:
        selected = pending.pop()
        if isinstance(selected, str):
            for _ in range(5):
                if password in selected:
                    raise ValueError("fixture observation contains private material")
                decoded = unquote_plus(selected, encoding="utf-8", errors="strict")
                if decoded == selected:
                    break
                selected = decoded
            else:
                raise ValueError("fixture observation encoding exceeds its safe bound")
        elif isinstance(selected, dict):
            pending.extend(selected.keys()); pending.extend(selected.values())
        elif isinstance(selected, list):
            pending.extend(selected)
    return value


def provision(*, password, candidate_source, candidate_image, sql=database_query, inspect=inspect_candidate,
              receipt=None, persist=lambda: None, clock=lambda: datetime.now(timezone.utc)):
    if not isinstance(password, str) or not password:
        raise ValueError("owned fixture database credential is missing")
    receipt = receipt if receipt is not None else {}
    started = clock().isoformat()
    before = inspect(candidate_source, candidate_image)
    receipt.update(schema="honua.mcp-ops-events-setup/v1", startedAt=started, runtimeBinding=before,
        setupSqlSha256="sha256:" + hashlib.sha256(SETUP_SQL.encode()).hexdigest(), phase="seed-pending")
    persist()
    raw = sql(SETUP_SQL, password)
    if not isinstance(raw, str) or len(raw.encode()) > 65536:
        raise ValueError("original owned SQL setup response is missing or oversized")
    seed = setup_seed(strict_json(raw))
    receipt.update(seed=seed, setupResponseSha256="sha256:" + hashlib.sha256(raw.encode()).hexdigest(),
                   setupResponseText=raw, phase="seeded")
    persist()
    original = observe(seed, password=password, candidate_source=candidate_source, candidate_image=candidate_image,
                       sql=sql, inspect=inspect, clock=clock)
    receipt.update(originalStore=original, phase="observed")
    persist()
    after = inspect(candidate_source, candidate_image)
    if before != after or before != original["runtimeBinding"]:
        raise ValueError("candidate changed during owned read fixture provisioning")
    receipt.update(completedAt=clock().isoformat(), phase="complete")
    persist()
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-source-sha", required=True)
    parser.add_argument("--candidate-image", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--owned-disposable-candidate", action="store_true", required=True)
    args = parser.parse_args()
    receipt = {}
    def persist():
        encoded = json.dumps(receipt, indent=2, allow_nan=False) + "\n"
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.receipt.with_suffix(args.receipt.suffix + ".partial")
        temporary.write_text(encoded)
        temporary.replace(args.receipt)
    try:
        if args.receipt.exists():
            raise ValueError("original fixture setup receipt already exists")
        provision(password=os.environ.get("HONUA_CERT_DB_PASSWORD"), receipt=receipt, persist=persist,
                  candidate_source=args.candidate_source_sha, candidate_image=args.candidate_image)
        return 0
    except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
        print("owned durable MCP read fixture setup refused", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
