"""MCP map tools address a publication index, never the storage id from publish."""
from pathlib import Path
import unittest

import mcp_layer_address as address


ROOT = Path(__file__).resolve().parent


class PublicationTargetTests(unittest.TestCase):
    def test_listed_row_supplies_the_index_and_ignores_the_storage_id(self):
        rows = [{
            "serviceId": "svc-sites", "serviceName": "sdkreg-sites", "serviceLayerIndex": 0,
            "storageLayerId": 741, "layerId": 741,
        }, {
            "serviceId": "svc-area", "serviceName": "sdkreg-area", "serviceLayerIndex": 2,
            "storageLayerId": 17,
        }]
        target = address.publication_target(rows, "sdkreg-sites")
        self.assertEqual(target, {"serviceId": "svc-sites", "serviceLayerIndex": 0})
        self.assertEqual(
            address.query_arguments(target, where="rank > 0", limit=1000),
            {"serviceId": "svc-sites", "serviceLayerIndex": 0, "where": "rank > 0", "limit": 1000})
        self.assertEqual(address.render_layer(target), {"serviceId": "svc-sites", "serviceLayerIndex": 0})

    def test_storage_id_and_a_bool_are_not_a_publication_index(self):
        row = {"serviceId": "svc-sites", "serviceName": "sdkreg-sites", "layerId": 741}
        with self.assertRaises(ValueError):
            address.publication_target([row], "sdkreg-sites")
        present = {**row, "serviceLayerIndex": True}
        with self.assertRaises(ValueError):
            address.publication_target([present], "sdkreg-sites")
        with self.assertRaises(ValueError):
            address.query_arguments({"serviceId": "svc-sites", "serviceLayerIndex": 0, "layerId": 741}, where="1=1", limit=1)

    def test_zero_or_two_rows_for_the_service_refuse(self):
        row = {"serviceId": "svc-sites", "serviceName": "sdkreg-sites", "serviceLayerIndex": 0}
        with self.assertRaises(ValueError):
            address.publication_target([], "sdkreg-sites")
        with self.assertRaises(ValueError):
            address.publication_target([row, dict(row)], "sdkreg-sites")

    def test_paging_stops_on_has_more_and_keeps_the_named_service(self):
        pages = [
            {"layers": [{"serviceId": "other", "serviceName": "other", "serviceLayerIndex": 4}],
             "hasMore": True, "nextOffset": 100},
            {"layers": [{"serviceId": "svc-sites", "serviceName": "sdkreg-sites", "serviceLayerIndex": 0,
                         "storageLayerId": 741}],
             "hasMore": False, "nextOffset": None},
        ]
        seen = []

        def call(arguments):
            seen.append(arguments)
            return pages[len(seen) - 1]

        target = address.address_for_service(call, "sdkreg-sites")
        self.assertEqual(seen, [
            {"filter": "sdkreg-sites", "limit": 100, "offset": 0},
            {"filter": "sdkreg-sites", "limit": 100, "offset": 100},
        ])
        self.assertEqual(target["serviceLayerIndex"], 0)
        self.assertNotIn("layerId", address.query_arguments(target, where="1=1", limit=1))

    def test_callers_no_longer_send_the_plan_storage_id(self):
        # SDK runners still pass layerId. These are the MCP tool argument shapes.
        forbidden = (
            '"layerId": area["layerId"]',
            '"layerId": state["layer"]',
            '"layerId": probe["layerId"]',
        )
        for relative in ("drivers/mcp/driver.py", "interop/orchestrator.py"):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for snippet in forbidden:
                self.assertNotIn(snippet, text, relative)
            self.assertIn("address_for_service", text, relative)
            self.assertIn("query_arguments", text, relative)
            self.assertIn("render_layer", text, relative)


if __name__ == "__main__":
    unittest.main()
