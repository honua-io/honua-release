"""Offline mutation controls for the independent owned Operate store reader.

These vectors are synthetic. Passing them never establishes a client run,
actual database receipt, or candidate qualification.
"""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import unittest

import mcp_ops_events_state as state


def seed():
    return {"profile": state.PROFILE, "sourceHydrated": True, "seededAt": "2026-10-19T22:00:00+00:00",
        "serviceId": "canonical-mcp-ops", "layerId": 1, "actor": "canonical-mcp-fixture",
        "alerts": {"openAlertEventId": 31, "resolvedAlertEventId": 32, "zoneId": 10, "ruleId": 20,
            "ruleName": "Canonical MCP Harbor Entry", "zoneName": "Canonical MCP Harbor",
            "dedupeKeys": [state.PROFILE + ":alert-open", state.PROFILE + ":alert-resolved"]}}


def store():
    setup = seed()
    timestamp = datetime(2026, 10, 19, 22, tzinfo=timezone.utc)
    rows = []
    for resolved in (False, True):
        rows.append({"eventId": 32 if resolved else 31,
            "dedupeKey": state.PROFILE + (":alert-resolved" if resolved else ":alert-open"),
            "ruleId": 20, "ruleName": setup["alerts"]["ruleName"], "zoneId": 10,
            "serviceId": setup["serviceId"], "layerId": 1, "objectId": 4202 if resolved else 4201,
            "triggerCode": 1, "severity": "critical" if resolved else "warning",
            "generation": 1209002 if resolved else 1209001,
            "occurredAt": (timestamp - timedelta(minutes=18 if resolved else 9)).isoformat(),
            "incidentCode": 3 if resolved else 1, "incidentDurationMs": 420000 if resolved else 0,
            "lifecycleCode": 3 if resolved else 0,
            "acknowledgedAt": None, "acknowledgedBy": None, "suppressedUntil": None,
            "resolvedAt": (timestamp - timedelta(minutes=7)).isoformat() if resolved else None,
            "resolvedBy": setup["actor"] if resolved else None})
    return {"profile": state.PROFILE, "observedAt": "2026-10-19T22:00:01+00:00",
        "databaseIdentity": {"clusterId": "12345678", "database": "honua_test", "user": "honua"}, "alerts": rows,
        "windowEventIds": [31, 32]}


class IndependentOperateStateControls(unittest.TestCase):
    def test_both_original_durable_rows_and_lifecycle_are_required(self):
        rows = state.fixture_rows(store(), seed())
        self.assertEqual({row["eventId"] for row in rows.values()}, {31, 32})
        self.assertEqual(rows[state.PROFILE + ":alert-resolved"]["resolvedBy"], "canonical-mcp-fixture")

    def test_original_setup_identity_cannot_be_skipped_or_substituted(self):
        mutations = [lambda s: s.update(sourceHydrated=False), lambda s: s.update(layerId=True),
            lambda s: s.update(serviceId="foreign"), lambda s: s.update(actor="foreign"),
            lambda s: s["alerts"].update(openAlertEventId=0),
            lambda s: s["alerts"].update(openAlertEventId=32),
            lambda s: s["alerts"].update(zoneName="foreign"),
            lambda s: s["alerts"].update(dedupeKeys=[]),
            lambda s: s.update(seededAt="2026-10-19T22:00:00"),
            lambda s: s.update(seededAt="2026-10-19T22:01:00Z")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                setup = seed(); mutate(setup)
                with self.assertRaises(ValueError):
                    state.fixture_rows(store(), setup)

    def test_store_identity_cannot_change_or_invent_cluster(self):
        for key, values in {"clusterId": ["0", "18446744073709551616", "１２３", True],
                            "database": ["foreign"], "user": ["foreign"]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    observed = store(); observed["databaseIdentity"][key] = value
                    with self.assertRaises(ValueError):
                        state.fixture_rows(observed, seed())

    def test_empty_duplicate_substituted_or_partial_rows_refuse(self):
        for mutation in (lambda v: v.update(alerts=[]),
                         lambda v: v["alerts"].append(copy.deepcopy(v["alerts"][0])),
                         lambda v: v["alerts"][1].update(dedupeKey=v["alerts"][0]["dedupeKey"]),
                         lambda v: v["alerts"][0].pop("generation")):
            observed = store(); mutation(observed)
            with self.assertRaises(ValueError):
                state.fixture_rows(observed, seed())

    def test_global_window_census_cannot_hide_ambient_or_missing_events(self):
        for identities in ([], [31], [31, 32, 33], [31, 31], [32, 31], ["31", 32], [True, 32]):
            with self.subTest(identities=identities):
                observed = store(); observed["windowEventIds"] = identities
                with self.assertRaises(ValueError): state.fixture_rows(observed, seed())

    def test_each_causal_column_is_adjudicated_independently(self):
        mutations = {"eventId": 900, "ruleId": 900, "zoneId": 900, "serviceId": "foreign",
            "layerId": True, "objectId": 900, "triggerCode": 2, "severity": "info", "generation": 0,
            "occurredAt": "2026-10-19T20:00:00Z", "incidentCode": 2, "incidentDurationMs": 1,
            "lifecycleCode": 1, "acknowledgedAt": "2026-10-19T21:00:00Z", "acknowledgedBy": "foreign",
            "suppressedUntil": "2026-10-20T00:00:00Z", "resolvedAt": "2026-10-19T21:00:00Z",
            "resolvedBy": "foreign", "ruleName": "foreign"}
        for index in (0, 1):
            for key, value in mutations.items():
                with self.subTest(row=index, key=key):
                    observed = store(); observed["alerts"][index][key] = value
                    with self.assertRaises(ValueError):
                        state.fixture_rows(observed, seed())

    def test_microsecond_storage_rounding_is_the_only_time_tolerance(self):
        observed = store()
        original = state.instant(observed["alerts"][0]["occurredAt"])
        observed["alerts"][0]["occurredAt"] = (original + timedelta(microseconds=1)).isoformat()
        state.fixture_rows(observed, seed())
        observed["alerts"][0]["occurredAt"] = (original + timedelta(microseconds=3)).isoformat()
        with self.assertRaises(ValueError):
            state.fixture_rows(observed, seed())

    def test_observer_reads_actual_fixed_sql_and_inspects_candidate_on_both_sides(self):
        calls = []
        def sql(query, password):
            self.assertEqual(query, state.STORE_SQL)
            self.assertEqual(password, "private-control-value")
            self.assertIn("SET TRANSACTION READ ONLY", query)
            return json.dumps(store())
        def inspect(source, image):
            calls.append((source, image))
            return {"controlledRuntime": "same"}
        clock = iter((state.instant("2026-10-19T22:00:00Z"), state.instant("2026-10-19T22:00:02Z")))
        result = state.observe(seed(), password="private-control-value", candidate_source="controlled-source",
            candidate_image="controlled-image", sql=sql, inspect=inspect, clock=lambda: next(clock))
        self.assertEqual(calls, [("controlled-source", "controlled-image")] * 2)
        self.assertEqual(result["store"], store())
        self.assertNotIn("private-control-value", json.dumps(result))

    def test_stale_or_future_probe_clock_cannot_be_a_fresh_observation(self):
        for timestamp in ("2026-10-19T21:59:59Z", "2026-10-19T22:00:03Z"):
            clock = iter((state.instant("2026-10-19T22:00:02Z"), state.instant("2026-10-19T22:00:02Z")))
            with self.assertRaises(ValueError):
                state.observe(seed(), password="private-control-value", candidate_source="source", candidate_image="image",
                    sql=lambda *args: json.dumps(store() | {"observedAt": timestamp}),
                    inspect=lambda *args: {}, clock=lambda: next(clock))

    def test_runtime_change_and_actual_store_failure_cannot_report_success(self):
        observations = iter(({"container": "before"}, {"container": "after"}))
        with self.assertRaisesRegex(ValueError, "runtime changed"):
            state.observe(seed(), password="private-control-value", candidate_source="source", candidate_image="image",
                sql=lambda *args: json.dumps(store()), inspect=lambda *args: next(observations))
        def failed(*args):
            raise ValueError("actual database failed")
        with self.assertRaisesRegex(ValueError, "database failed"):
            state.observe(seed(), password="private-control-value", candidate_source="source", candidate_image="image",
                sql=failed, inspect=lambda *args: {})


if __name__ == "__main__":
    unittest.main()
