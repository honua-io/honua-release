"""Synthetic provisioning controls; actual SQL and candidate proof stay separate."""
from __future__ import annotations

import copy
import json
import unittest
from urllib.parse import quote

import mcp_ops_events_setup as setup
import mcp_ops_events_state as state
from mcp_ops_events_state_controls import seed, store


def fixture_seed():
    return seed() | {"setupMethod": "owned-sql-read-fixture", "evaluationExercised": False,
        "deliveryExercised": False, "lifecycleApiExercised": False}


def clock():
    values = iter(map(state.instant, ("2026-10-19T21:59:59Z", "2026-10-19T22:00:00Z",
        "2026-10-19T22:00:02Z", "2026-10-19T22:00:03Z")))
    return lambda: next(values)


class OwnedReadSetupControls(unittest.TestCase):
    def test_canonical_public_password_does_not_reject_its_own_schema_image_or_database_identity(self):
        from mcp_render_validation_controls import runtime
        observed = setup.provision(password="honua", candidate_source="b" * 40,
            candidate_image=runtime()["candidateImage"], inspect=lambda *args: runtime(), clock=clock(),
            sql=lambda query, password: json.dumps(fixture_seed() if query == setup.SETUP_SQL else store()))
        self.assertEqual(observed["phase"], "complete")
        self.assertEqual(observed["originalStore"]["store"]["databaseIdentity"]["user"], "honua")
        self.assertIn("honua-io", observed["runtimeBinding"]["candidateImage"])

    def test_fixed_seed_then_independent_read_and_runtime_are_retained_in_order(self):
        statements, phases, receipt = [], [], {}
        def sql(query, password):
            self.assertEqual(password, "private-control-value")
            statements.append(query)
            return json.dumps(fixture_seed() if query == setup.SETUP_SQL else store())
        result = setup.provision(password="private-control-value", candidate_source="source", candidate_image="image",
            sql=sql, inspect=lambda *args: {"controlledRuntime": "same"}, receipt=receipt,
            persist=lambda: phases.append(receipt["phase"]), clock=clock())
        self.assertEqual(statements, [setup.SETUP_SQL, state.STORE_SQL])
        self.assertEqual(phases, ["seed-pending", "seeded", "observed", "complete"])
        self.assertEqual(result["seed"], fixture_seed())
        self.assertEqual(result["originalStore"]["store"], store())
        self.assertEqual(json.loads(result["setupResponseText"]), fixture_seed())
        self.assertNotIn("private-control-value", json.dumps(result))

    def test_store_failure_after_seed_preserves_original_allocated_receipt(self):
        receipt, retained = {}, []
        def sql(query, password):
            if query == setup.SETUP_SQL:
                return json.dumps(fixture_seed())
            raise ValueError("actual read failed")
        with self.assertRaisesRegex(ValueError, "read failed"):
            setup.provision(password="private-control-value", candidate_source="source", candidate_image="image", sql=sql,
                inspect=lambda *args: {}, receipt=receipt, persist=lambda: retained.append(copy.deepcopy(receipt)), clock=clock())
        self.assertEqual(retained[-1]["phase"], "seeded")
        self.assertEqual(retained[-1]["seed"], fixture_seed())
        self.assertNotIn("originalStore", retained[-1])

    def test_seed_failure_cannot_report_a_completed_or_observed_fixture(self):
        receipt = {}
        def sql(*args):
            raise ValueError("existing fixture refuses reseed")
        with self.assertRaisesRegex(ValueError, "refuses reseed"):
            setup.provision(password="private-control-value", candidate_source="source", candidate_image="image", sql=sql,
                inspect=lambda *args: {}, receipt=receipt, clock=clock())
        self.assertEqual(receipt["phase"], "seed-pending")
        self.assertNotIn("seed", receipt)
        self.assertNotIn("completedAt", receipt)

    def test_no_evaluation_delivery_or_lifecycle_api_credit_can_be_invented(self):
        for field in ("evaluationExercised", "deliveryExercised", "lifecycleApiExercised"):
            value = fixture_seed(); value[field] = True
            with self.subTest(field=field), self.assertRaises(ValueError):
                setup.provision(password="private-control-value", candidate_source="source", candidate_image="image",
                    sql=lambda *args: json.dumps(value), inspect=lambda *args: {}, clock=clock())

    def test_secret_echo_is_refused_before_original_response_retention(self):
        secret = "private-control-value"
        for leaked in (secret, quote(secret, safe="").replace("-", "%2D"), "".join(f"%{ord(c):02x}" for c in secret)):
            receipt = {}; value = fixture_seed() | {"unexpected": {"echo": leaked}}
            with self.assertRaises(ValueError):
                setup.provision(password=secret, candidate_source="source", candidate_image="image",
                    sql=lambda *args: json.dumps(value), inspect=lambda *args: {}, receipt=receipt, clock=clock())
            self.assertNotIn("seed", receipt)
            self.assertNotIn(secret, json.dumps(receipt))

    def test_unknown_or_changed_seed_fields_refuse_before_raw_response_retention(self):
        mutations = (lambda v: v["alerts"].update(echo="private-control-value"),
            lambda v: v.update(extra="%70%72%69%76%61%74%65"),
            lambda v: v.update(actor="private-control-value"),
            lambda v: v.update(seededAt="private-control-value"),
            lambda v: v.update(layerId=True),
            lambda v: v["alerts"].update(openAlertEventId=9223372036854775808),
            lambda v: v["alerts"].update(dedupeKeys=["private-control-value"]))
        for mutate in mutations:
            value = fixture_seed(); mutate(value); receipt = {}
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                setup.provision(password="honua", candidate_source="source", candidate_image="image",
                    sql=lambda *args: json.dumps(value), inspect=lambda *args: {}, receipt=receipt, clock=clock())
            self.assertNotIn("seed", receipt)
            self.assertNotIn("setupResponseText", receipt)

    def test_missing_credential_never_inspects_or_mutates(self):
        def forbidden(*args):
            self.fail("a missing credential must fail before any external operation")
        with self.assertRaises(ValueError):
            setup.provision(password=None, candidate_source="source", candidate_image="image", sql=forbidden, inspect=forbidden)


if __name__ == "__main__":
    unittest.main()
