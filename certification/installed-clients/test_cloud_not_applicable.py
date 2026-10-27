"""Founder ruling cloud-cli-workflow-na-2026-10-27: cli-workflow is declared not-applicable on GA cloud cells."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import yaml

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("installed_cert_na", HERE / "run.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

GA_CELL = "aws-ecs/redis-on"
NA_CELLS = {"npm-cli-workflow", "pypi-cli-workflow"}


def inputs():
    return (yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text()),
            json.loads((HERE / "matrix.json").read_text()))


def results(matrix, deployment_cell, ruling, *, na=NA_CELLS):
    """Every committed cell at its expected outcome, with `na` reported not-applicable."""
    rows = []
    for cell in matrix["cells"]:
        row = {"cell": cell["id"], "status": "blocked" if cell["status"] == "blocked" else "pass"}
        if cell["status"] == "blocked":
            row.update(blockedBy=cell["blockedBy"], declared=mod.declaration(cell))
        if cell["id"] in na:
            row = {"cell": cell["id"], "status": mod.NOT_APPLICABLE,
                   "notApplicable": {"ruling": mod.CLOUD_NOT_APPLICABLE_RULING, "deploymentCell": deployment_cell,
                                     "coveredBy": list((ruling or {}).get("coveredBy", ["x"]))}}
        rows.append(row)
    return rows


def ruling_file(**changes):
    ruling = {"owner": "Mike McDougall (founder)", "installed_client_cells": sorted(NA_CELLS),
              "deployment_cells": [GA_CELL], "covered_by": ["journey stages 7-8", "local regression stack"],
              **changes}
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"rulings": {mod.CLOUD_NOT_APPLICABLE_RULING: ruling}}, handle)
    handle.close()
    return Path(handle.name)


class CloudNotApplicableTests(unittest.TestCase):
    def status(self, rows, deployment_cell, ruling):
        _, matrix = inputs()
        return mod.receipt_status(rows, mod.ruled_declarations(), matrix=matrix,
                                  deployment_cell=deployment_cell, not_applicable=ruling)

    def test_committed_ruling_covers_exactly_the_cli_workflow_cells_on_the_ga_cell(self):
        ruling = mod.ruled_cloud_not_applicable()
        self.assertEqual(ruling["cells"], NA_CELLS)
        self.assertEqual(ruling["deploymentCells"], {GA_CELL})
        self.assertTrue(ruling["deploymentCells"] <= mod.GA_CLOUD_CELLS)
        _, matrix = inputs()
        self.assertEqual({c["scenario"] for c in matrix["cells"] if c["id"] in NA_CELLS}, {"cli-workflow"})
        self.assertEqual(len(ruling["coveredBy"]), 2)

    def test_green_ruled_not_applicable_rows_pass_on_the_ga_cloud_cell(self):
        ruling = mod.ruled_cloud_not_applicable()
        _, matrix = inputs()
        self.assertEqual(self.status(results(matrix, GA_CELL, ruling), GA_CELL, ruling), "pass")

    def test_red_not_applicable_off_the_ga_cloud_cells_fails(self):
        ruling = mod.ruled_cloud_not_applicable()
        _, matrix = inputs()
        for deployment in (None, "aws-serverless/redis-on", "aws-ecs/redis-off", "local-docker"):
            with self.subTest(deployment=deployment):
                self.assertEqual(self.status(results(matrix, deployment, ruling), deployment, ruling), "fail")

    def test_red_not_applicable_without_its_ruling_fails(self):
        _, matrix = inputs()
        ruling = mod.ruled_cloud_not_applicable()
        self.assertEqual(self.status(results(matrix, GA_CELL, ruling), GA_CELL, {}), "fail")
        missing = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump({"rulings": {}}, missing)
        missing.close()
        self.assertEqual(mod.ruled_cloud_not_applicable(Path(missing.name)), {})

    def test_red_a_widened_or_non_founder_ruling_is_void(self):
        for changes in ({"deployment_cells": [GA_CELL, "aws-serverless/redis-on"]},
                        {"deployment_cells": ["local-docker"]},
                        {"owner": "release coordinator"},
                        {"covered_by": []},
                        {"installed_client_cells": []}):
            with self.subTest(changes=changes):
                self.assertEqual(mod.ruled_cloud_not_applicable(ruling_file(**changes)), {})

    def test_red_not_applicable_on_a_cell_the_ruling_does_not_cover_fails(self):
        ruling = mod.ruled_cloud_not_applicable()
        _, matrix = inputs()
        rows = results(matrix, GA_CELL, ruling, na=NA_CELLS | {"npm-sdk-auth"})
        self.assertEqual(self.status(rows, GA_CELL, ruling), "fail")
        # A ruling naming a non-cli-workflow cell does not make that cell not-applicable either.
        widened = mod.ruled_cloud_not_applicable(ruling_file(installed_client_cells=sorted(NA_CELLS | {"npm-sdk-auth"})))
        self.assertNotIn("npm-sdk-auth", mod.not_applicable_cells(matrix, GA_CELL, widened))

    def test_red_a_not_applicable_row_must_carry_the_ruling_binding(self):
        ruling = mod.ruled_cloud_not_applicable()
        _, matrix = inputs()
        for defect in ("drop", "other-ruling", "other-cell", "steps"):
            rows = results(matrix, GA_CELL, ruling)
            row = next(r for r in rows if r["cell"] == "npm-cli-workflow")
            if defect == "drop":
                row.pop("notApplicable")
            elif defect == "other-ruling":
                row["notApplicable"]["ruling"] = "dotnet-declared-blocked-2026-10-26"
            elif defect == "other-cell":
                row["notApplicable"]["deploymentCell"] = "aws-ecs/redis-off"
            else:
                row["steps"] = []
            with self.subTest(defect=defect):
                self.assertEqual(self.status(rows, GA_CELL, ruling), "fail")

    def test_verify_receipt_holds_the_not_applicable_rows_to_the_ruling(self):
        manifest, matrix = inputs()
        ruling = mod.ruled_cloud_not_applicable()
        receipt = mod.make_receipt(manifest, matrix, results(matrix, GA_CELL, ruling), "https://example.invalid/run",
                                   deployment_cell=GA_CELL, not_applicable=ruling)
        receipt["deployment"] = {"cell": GA_CELL}
        self.assertEqual(receipt["status"], "pass")
        self.assertEqual({row["cell"] for row in receipt["notApplicable"]}, NA_CELLS)
        na_violations = lambda r: [v for v in mod.verify_receipt(matrix, r, not_applicable=ruling)  # noqa: E731
                                   if "not-applicable" in v or "notApplicable" in v or "active cell is" in v
                                   or "does not follow" in v]
        self.assertEqual(na_violations(receipt), [])
        moved = copy.deepcopy(receipt)
        moved["deployment"] = {"cell": "aws-serverless/redis-on"}
        self.assertTrue(na_violations(moved))
        self.assertTrue(na_violations(receipt) == [] and mod.verify_receipt(matrix, receipt, not_applicable={}))

    def test_execute_leaves_ruled_cells_out_of_the_suite_only_on_the_ga_cloud_cell(self):
        manifest, matrix = inputs()
        for deployment, excluded in ((GA_CELL, NA_CELLS), ("aws-serverless/redis-on", set())):
            seen = {}

            def suite(manifest, suite_matrix, base, context=None):
                seen["cells"] = {cell["id"] for cell in suite_matrix["cells"]}
                return {cell["id"]: ("pass", "ok", []) for cell in suite_matrix["cells"] if cell["driver"] in mod.SUITE_DRIVERS}

            def cell_run(cell, *args):
                return (f"blocked:{cell['blockedBy']}" if cell["status"] == "blocked" else "pass"), "ok"

            context = {"binding": {"cell": deployment, "server": {"sourceSha": "s", "image": "i"}}}
            with (mock.patch.object(mod, "run_suite", suite), mock.patch.object(mod, "run_cell", cell_run)):
                receipt = mod.execute(manifest, matrix, "https://example.invalid/run", endpoint_context=context)
            with self.subTest(deployment=deployment):
                self.assertFalse(seen["cells"] & excluded)
                na = {row["cell"] for row in receipt["results"] if row["status"] == mod.NOT_APPLICABLE}
                self.assertEqual(na, excluded)
                for row in receipt["results"]:
                    if row["cell"] in na:
                        self.assertEqual(row["notApplicable"]["ruling"], mod.CLOUD_NOT_APPLICABLE_RULING)
                        self.assertIn("journey stages 7-8", row["detail"])

    def test_a_local_run_never_reports_not_applicable(self):
        manifest, matrix = inputs()
        with (mock.patch.object(mod, "run_suite", lambda m, mx, b, c=None: {
                    cell["id"]: ("pass", "ok", []) for cell in mx["cells"] if cell["driver"] in mod.SUITE_DRIVERS}),
              mock.patch.object(mod, "run_cell", lambda cell, *a: (
                    (f"blocked:{cell['blockedBy']}" if cell["status"] == "blocked" else "pass"), "ok"))):
            receipt = mod.execute(manifest, matrix, "https://example.invalid/run")
        self.assertFalse([row for row in receipt["results"] if row["status"] == mod.NOT_APPLICABLE])


if __name__ == "__main__":
    unittest.main()
