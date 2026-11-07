#!/usr/bin/env python3
"""Replay actual honua-server 86286687 observations through the corrected judges.

mcp-6901-replay-41680771276.json is a trimmed copy of the retained raw evidence of
geospatial-mcp certify run 41680771276 (both distributed clients). It keeps only what these
judges read: every non-render tool result of the style, ingest, findings/events, health,
grounding and publish-result journeys, the declared serving projection, and the publication's
new service-layer rows. That run failed on three server contracts the judges did not model:
  * honua-server #6901 returns [json] or [summary, json] text content;
  * honua-server #6851 writes serving observationStatus;
  * a new service's only layer is ordered 1 (PostgreSqlLayerPublishingService.LayerPersistence.cs:819).
"""
from __future__ import annotations

import collections
import copy
import json
from pathlib import Path
import unittest

import mcp_grounding_status_validation as grounding
import mcp_map_package_validation as wire
import mcp_ops_events_validation as events
import mcp_ops_health_validation as health
import mcp_publish_result_validation as publication
import mcp_style_validation as style

FIXTURE = json.loads(Path(__file__).with_name("mcp-6901-replay-41680771276.json").read_text(encoding="utf-8"))
QUERY_FIXTURE = json.loads(Path(__file__).with_name("mcp-publish-query-replay-41714656270.json").read_text(encoding="utf-8"))
SUMMARIES = {
    "honua_ops_health": "Ops health snapshot returned in structuredContent.",
    "honua_alert_events": "Alert-event page returned in structuredContent.",
    "honua_operate_events": "Operate-event page returned in structuredContent.",
    "honua_ops_findings": "Ops findings returned in structuredContent.",
}


def results():
    for client, families in FIXTURE["clients"].items():
        for family, data in families.items():
            for item in data["toolResults"]:
                yield client, family, item["tool"], item["result"]


class ActualServerReplay(unittest.TestCase):
    def test_retained_publication_queries_preserve_source_ids_and_attributes(self):
        self.assertEqual(QUERY_FIXTURE["source"]["workflowRun"], 41714656270)
        self.assertEqual(QUERY_FIXTURE["source"]["serverSourceSha"], "8628668794df9f0c22a490e59cedaee409cc1bdd")
        self.assertEqual(QUERY_FIXTURE["source"]["artifactId"], 13270967941)
        self.assertEqual(set(QUERY_FIXTURE["clients"]), {"sdk", "inspector"})
        for client, observations in QUERY_FIXTURE["clients"].items():
            self.assertEqual(len(observations["queryResults"]), 2)
            for name, result in zip(("fresh", "restarted"), observations["queryResults"]):
                with self.subTest(client=client, query=name):
                    publication.query_output(result, {}, observations["database"])

    def test_fixture_is_the_retained_candidate_evidence(self):
        self.assertEqual(FIXTURE["source"]["workflowRun"], 41680771276)
        self.assertEqual(FIXTURE["source"]["serverSourceSha"], "8628668794df9f0c22a490e59cedaee409cc1bdd")
        self.assertEqual(set(FIXTURE["clients"]), {"sdk", "inspector"})

    def test_every_actual_tool_result_has_the_server_text_shape(self):
        shapes = collections.Counter()
        for client, family, tool, result in results():
            with self.subTest(client=client, family=family, tool=tool):
                summary = False if result.get("isError") else SUMMARIES.get(tool)
                self.assertTrue(wire.same_json(wire.structured_text_content(result, summary=summary),
                                               result["structuredContent"]))
                shapes[(bool(result.get("isError")), len(result["content"]))] += 1
        # Successes arrive in both forms; errors are never summarized.
        self.assertGreater(shapes[(False, 1)], 0)
        self.assertGreater(shapes[(False, 2)], 0)
        self.assertGreater(shapes[(True, 1)], 0)
        self.assertEqual(shapes[(True, 2)], 0)

    def test_family_judges_accept_the_actual_successes(self):
        judged = collections.Counter()
        for client, family, tool, result in results():
            if result.get("isError"):
                continue
            with self.subTest(client=client, tool=tool):
                value = copy.deepcopy(result)
                if tool == "honua_ops_health":
                    health.tool_output(value)
                elif tool in ("honua_get_style", "honua_apply_style_preset"):
                    style.tool_output(value)
                elif tool in SUMMARIES:
                    events.payload(value, SUMMARIES[tool])
                elif family == "grounding":
                    grounding.successful_tool(value)
                else:
                    wire.structured_text_content(value)
                judged[family] += 1
        self.assertEqual(set(judged), {"style", "ingest", "opsEvents", "opsHealth", "grounding", "publishResult"})

    def test_actual_summarized_results_were_what_the_single_block_rule_refused(self):
        summarized = [(tool, result) for *_, tool, result in results() if len(result["content"]) == 2]
        self.assertTrue({"honua_ops_health", "honua_query_features", "honua_ops_findings"}
                        <= {tool for tool, _ in summarized})
        for tool, result in summarized:
            with self.subTest(tool=tool):
                self.assertGreaterEqual(len(result["content"][1]["text"]), wire.INLINE_TEXT_THRESHOLD)

    def test_mutations_of_actual_results_are_refused(self):
        for client, family, tool, result in results():
            if len(result["content"]) != 2:
                continue
            summary = SUMMARIES.get(tool)
            mismatch = json.loads(result["content"][1]["text"])
            mismatch["replayMutation"] = True
            changes = {
                "summary-only": lambda r: r.update(content=r["content"][:1]),
                "three-items": lambda r: r["content"].append(dict(r["content"][1])),
                "json-mismatch": lambda r: r["content"][1].update(text=json.dumps(mismatch)),
                "json-in-summary-slot": lambda r: r["content"][0].update(text=r["content"][1]["text"]),
                "error-summarized": lambda r: r.update(isError=True),
            }
            if summary:
                changes["pinned-summary-missing"] = lambda r: r.update(content=r["content"][1:])
                changes["pinned-summary-differs"] = lambda r: r["content"][0].update(text="Success.")
            for name, change in changes.items():
                with self.subTest(client=client, tool=tool, mutation=name):
                    changed = copy.deepcopy(result)
                    change(changed)
                    with self.assertRaises(ValueError):
                        wire.structured_text_content(changed, summary=False if changed.get("isError") else summary)

    def test_actual_serving_observation_status(self):
        for client, families in FIXTURE["clients"].items():
            for entries in families["grounding"]["serving"]:
                for entry in entries:
                    with self.subTest(client=client):
                        self.assertEqual(grounding.serving_projection(copy.deepcopy(entry))["observationStatus"], "notObserved")
                        with self.assertRaises(ValueError):  # the #1232 rule this corrects
                            grounding.plane_projection(copy.deepcopy(entry))
                        for status in ("healthy", None, "NotObserved", ""):
                            with self.assertRaises(ValueError):
                                grounding.serving_projection(dict(entry, observationStatus=status))
                        with self.assertRaises(ValueError):
                            grounding.serving_projection({k: v for k, v in entry.items() if k != "observationStatus"})

    def test_actual_new_service_layer_order(self):
        for client, families in FIXTURE["clients"].items():
            rows = families["publishResult"]["newServiceLayers"]
            self.assertEqual(len(rows), 1, client)
            for row in rows:
                publication.new_service_relation(copy.deepcopy(row))
                for order in (0, 2, None, "1", True):
                    with self.subTest(client=client, order=order), self.assertRaises(ValueError):
                        publication.new_service_relation(dict(row, layer_order=order))


if __name__ == "__main__":
    unittest.main()
