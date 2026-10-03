import copy
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest import mock

import yaml

HERE = Path(__file__).parent
EVIDENCE = "https://github.com/honua-io/honua-sdk-dotnet/actions/runs/1"
SERVICE_CASES = (
    "esri_census_states",
    "esri_military_ops_area_z",
    "esri_military_ops_line_zm",
    "esri_usa_cities",
    "esri_usa_highways",
    "esri_wildfire_lines",
    "hawaii_historiccultural_moku",
    "hawaii_infra_dams",
    "hawaii_infra_marine_sewerlines",
    "kauai_bridges",
)
CHECKS = (
    "all_fields_diff",
    "core_query_parity",
    "distinct_parity",
    "edge_case_query_parity",
    "error_shape_parity",
    "geojson_query_parity",
    "geometry_parity",
    "grouped_statistics_parity",
    "mapserver_featureserver_parity",
    "spatial_envelope_parity",
    "statistics_parity",
    "time_query_parity",
)
JOURNEY = (
    "journey.published-package-install",
    "journey.inventory-discovery",
    "journey.service-selection",
    "journey.apply",
    "journey.wait",
    "journey.cancellation-recovery",
    "journey.reconciliation",
    "fixture.points",
    "fixture.lines",
    "fixture.polygons",
    "fixture.tables",
    "fixture.domains-subtypes",
    "fixture.related-records",
    "fixture.attachments",
    "fixture.zm-curves",
    "fixture.nulls-int64-time",
    "fixture.renderer-time-metadata",
    "fixture.filters-crs",
    "fixture.auth",
    "fixture.pagination-limits",
    "fixture.bounded-large-data",
    "fixture.injected-failure",
    "crosscheck.counts-values",
    "crosscheck.geometry-tolerances",
    "crosscheck.schema-metadata",
    "crosscheck.attachment-hashes",
    "crosscheck.relationships",
    "crosscheck.honua-and-esri-clients",
    "evidence.immutable-identities",
    "evidence.rollback-repoint",
    "evidence.rejects-data-loss",
    "dependency.server-4420",
)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("dotnet_import_fidelity", "dotnet_import_fidelity.py")
installed = _load("installed_cert_for_import", "run.py")


def _inputs():
    manifest = yaml.safe_load((HERE.parents[1] / "platform-manifest.yaml").read_text())
    requirements = json.loads((HERE / "dotnet-import-fidelity.requirements.v1.json").read_text())
    return manifest, requirements


def _cell(requirement_id: str, kind: str) -> dict:
    if kind == "not-applicable":
        return {"id": requirement_id, "verdict": "not-applicable", "executed": False, "stale": False, "sourceBuilt": False}
    return {
        "id": requirement_id,
        "verdict": "pass",
        "executed": True,
        "stale": False,
        "sourceBuilt": False,
        "evidenceUri": EVIDENCE,
    }


def _passing_receipt(manifest, requirements) -> dict:
    pin = manifest["clientArtifacts"]["honua-sdk-dotnet"]
    sha, image = gate.server_image_ref(manifest)
    cells = [_cell(cell["id"], cell["kind"]) for cell in requirements["cells"]]
    supported = sum(1 for cell in requirements["cells"] if cell["kind"] == "supported")
    not_applicable = sum(1 for cell in requirements["cells"] if cell["kind"] == "not-applicable")
    return {
        "schema": gate.RECEIPT_SCHEMA,
        "requirementsRevision": requirements["revision"],
        "generatedAt": "2026-09-26T18:00:00Z",
        "evidenceUri": EVIDENCE,
        "consumer": {
            "kind": "published-nuget",
            "packageId": pin["package"],
            "version": pin["version"],
            "digest": pin["digest"],
            "registry": "https://api.nuget.org/v3/index.json",
            "sourceBuilt": False,
            "clean": True,
            "projectReference": False,
            "assemblyMatchesPackage": True,
        },
        "server": {"sourceSha": sha, "image": image},
        "postgis": True,
        "sdkSeamMocks": False,
        "rollbackRepoint": "pass",
        "differences": [],
        "summary": {
            "denominator": supported,
            "passed": supported,
            "failed": 0,
            "skipped": 0,
            "notApplicable": not_applicable,
        },
        "cells": cells,
    }


class ImportFidelityGateTests(unittest.TestCase):
    def test_requirements_preserve_frozen_denominator(self):
        _, requirements = _inputs()
        self.assertEqual(requirements["cells"], gate.expected_requirement_cells())
        self.assertEqual(requirements["revision"], gate.canonical_revision(requirements["cells"]))
        self.assertEqual(requirements["publicRegistries"], ["https://api.nuget.org/v3/index.json"])
        supported = {cell["id"] for cell in requirements["cells"] if cell["kind"] == "supported"}
        not_applicable = {cell["id"] for cell in requirements["cells"] if cell["kind"] == "not-applicable"}
        baseline = {f"{case}/{check}" for case in SERVICE_CASES for check in CHECKS if check != "time_query_parity"}
        self.assertEqual(len(SERVICE_CASES), 10)
        self.assertEqual(len(CHECKS), 12)
        self.assertEqual(baseline, {cell["id"] for cell in requirements["cells"] if cell["group"] == "baseline" and cell["kind"] == "supported"})
        self.assertEqual(len(baseline), 110)
        self.assertEqual(not_applicable, {f"{case}/time_query_parity" for case in SERVICE_CASES})
        self.assertEqual(len(not_applicable), 10)
        self.assertEqual(supported - baseline, set(JOURNEY))
        self.assertEqual(len(JOURNEY), 32)
        self.assertEqual(len(supported), 142)
        self.assertIn("https://github.com/honua-io/honua-server/issues/4420", requirements["dependencies"])

    def test_matching_receipt_passes_and_missing_receipt_does_not(self):
        manifest, requirements = _inputs()
        passed = gate.evaluate(manifest, _passing_receipt(manifest, requirements), requirements)
        self.assertEqual(passed["findings"], [], passed["reason"])
        self.assertEqual(passed["status"], "pass")
        self.assertEqual(passed["passed"], 142)
        self.assertEqual(passed["denominator"], 142)
        missing = gate.evaluate(manifest, None, requirements)
        self.assertEqual(missing["status"], "fail")
        self.assertIn("missing evidence is not a pass", missing["reason"])

    def test_source_built_local_and_private_registries_fail(self):
        manifest, requirements = _inputs()
        base = _passing_receipt(manifest, requirements)
        cases = {
            "source-built": (lambda receipt: receipt["consumer"].update(kind="source-built", sourceBuilt=True), "source-built"),
            "local-dir": (lambda receipt: receipt["consumer"].update(localPackageDir="/tmp/packages"), "source-built"),
            "github-packages": (lambda receipt: receipt["consumer"].update(registry="github-packages"), "registry"),
            "file-feed": (lambda receipt: receipt["consumer"].update(registry="file:///tmp/nupkgs"), "registry"),
        }
        for name, (mutate, needle) in cases.items():
            with self.subTest(name=name):
                receipt = copy.deepcopy(base)
                mutate(receipt)
                verdict = gate.evaluate(manifest, receipt, requirements)
                self.assertEqual(verdict["status"], "fail")
                self.assertTrue(any(item["check"] == needle for item in verdict["findings"]), verdict["reason"])

    def test_wrong_pin_server_mocks_and_drift_fail(self):
        manifest, requirements = _inputs()
        base = _passing_receipt(manifest, requirements)

        def wrong_digest(receipt):
            receipt["consumer"]["digest"] = "sha256:" + "ab" * 32

        def wrong_version(receipt):
            receipt["consumer"]["version"] = "1.10.0"

        def wrong_server(receipt):
            receipt["server"]["image"] = receipt["server"]["image"].replace("069f196b", "00000000")

        def mocks(receipt):
            receipt["sdkSeamMocks"] = True

        def no_postgis(receipt):
            receipt["postgis"] = False

        def drift(receipt):
            receipt["differences"] = [{"code": "metadata-drift", "severity": "info"}]

        def blocking_loss(receipt):
            receipt["differences"] = [{"code": "catalog-reconciliation-failed", "severity": "blocking"}]

        for name, mutate, needle in (
            ("digest", wrong_digest, "wrong-version"),
            ("version", wrong_version, "wrong-version"),
            ("server", wrong_server, "server"),
            ("mocks", mocks, "sdk-seam-mocks"),
            ("postgis", no_postgis, "postgis"),
            ("drift", drift, "data-loss"),
            ("blocking", blocking_loss, "data-loss"),
        ):
            with self.subTest(name=name):
                receipt = copy.deepcopy(base)
                mutate(receipt)
                verdict = gate.evaluate(manifest, receipt, requirements)
                self.assertEqual(verdict["status"], "fail")
                self.assertTrue(any(item["check"] == needle for item in verdict["findings"]), verdict["reason"])

    def test_skipped_waived_released_stale_and_unknown_are_not_passes(self):
        manifest, requirements = _inputs()
        base = _passing_receipt(manifest, requirements)
        target = "esri_census_states/all_fields_diff"
        for verdict_name in ("skipped", "waived", "released", "unknown"):
            with self.subTest(verdict=verdict_name):
                receipt = copy.deepcopy(base)
                cell = next(item for item in receipt["cells"] if item["id"] == target)
                cell["verdict"] = verdict_name
                verdict = gate.evaluate(manifest, receipt, requirements)
                self.assertEqual(verdict["status"], "fail")
                self.assertTrue(any(verdict_name in item["why"] for item in verdict["findings"]), verdict["reason"])
                self.assertLess(verdict["passed"], verdict["denominator"])

        receipt = copy.deepcopy(base)
        cell = next(item for item in receipt["cells"] if item["id"] == target)
        cell["stale"] = True
        verdict = gate.evaluate(manifest, receipt, requirements)
        self.assertTrue(any(item["why"] == "stale" for item in verdict["findings"]), verdict["reason"])

    def test_not_applicable_and_shrunk_denominator_cannot_certify_100_percent(self):
        manifest, requirements = _inputs()
        receipt = _passing_receipt(manifest, requirements)
        cell = next(item for item in receipt["cells"] if item["id"] == "kauai_bridges/time_query_parity")
        cell["verdict"] = "pass"
        cell["executed"] = True
        cell["evidenceUri"] = EVIDENCE
        verdict = gate.evaluate(manifest, receipt, requirements)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(any("counted as pass" in item["why"] for item in verdict["findings"]), verdict["reason"])
        self.assertEqual(verdict["denominator"], 142)

        receipt = _passing_receipt(manifest, requirements)
        receipt["cells"] = [item for item in receipt["cells"] if item["id"] != "journey.apply"]
        receipt["summary"] = {"denominator": 141, "passed": 141, "failed": 0, "skipped": 0, "notApplicable": 10}
        receipt["denominator"] = 141
        verdict = gate.evaluate(manifest, receipt, requirements)
        self.assertEqual(verdict["status"], "fail")
        self.assertEqual(verdict["denominator"], 142)
        self.assertTrue(any(item["check"] == "journey.apply" and item["why"] == "missing" for item in verdict["findings"]))
        self.assertTrue(any(item["check"] == "denominator" for item in verdict["findings"]))

        shrunk = copy.deepcopy(requirements)
        shrunk["cells"] = [item for item in shrunk["cells"] if item["id"] != "esri_census_states/geometry_parity"]
        shrunk["revision"] = gate.canonical_revision(shrunk["cells"])
        verdict = gate.evaluate(manifest, _passing_receipt(manifest, requirements), shrunk)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(any("frozen baseline" in item["why"] for item in verdict["findings"]), verdict["reason"])

    def test_source_read_receipt_is_not_this_gate(self):
        manifest, requirements = _inputs()
        verdict = gate.evaluate(manifest, {
            "schema": "honua-sdk-dotnet.source-import-certification.v1",
            "summary": {"verdict": "certified", "pass": 41, "fail": 0, "released": 3},
        }, requirements)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(any(item["check"] == "schema" for item in verdict["findings"]), verdict["reason"])

    def _execute(self, matrix, receipt=None):
        manifest, _requirements = _inputs()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            installed, "install_npm", return_value=(True, "ok")
        ), mock.patch.object(installed, "install_pypi", return_value=(True, "ok")), mock.patch.object(
            installed, "install_nuget", return_value=(True, "ok")
        ):
            result = installed.execute(manifest, matrix, EVIDENCE, import_fidelity_receipt=receipt)
        row = next(item for item in result["results"] if item["cell"] == "nuget-service-layer-import-fidelity")
        return result, row

    @staticmethod
    def _matrices():
        committed = json.loads((HERE / "matrix.json").read_text())
        active = copy.deepcopy(committed)
        cell = next(item for item in active["cells"] if item["id"] == "nuget-service-layer-import-fidelity")
        cell["status"] = "active"
        del cell["blockedBy"]
        return committed, active

    def test_installed_client_cell_fails_closed_without_a_receipt(self):
        committed, active = self._matrices()
        receipt, row = self._execute(active)
        self.assertEqual(row["status"], "fail")
        self.assertIn("missing evidence is not a pass", row["detail"])
        self.assertEqual(receipt["status"], "fail")
        # The committed matrix blocks this cell on the missing receipt producer (honua-release#418).
        receipt, row = self._execute(committed)
        self.assertEqual(row["status"], "blocked")
        self.assertEqual(row["blockedBy"], "https://github.com/honua-io/honua-release/issues/418")
        self.assertIn("missing evidence is not a pass", row["detail"])
        self.assertEqual(receipt["status"], "blocked")

    def test_installed_client_cell_accepts_only_a_matching_receipt(self):
        manifest, requirements = _inputs()
        committed, active = self._matrices()
        good = _passing_receipt(manifest, requirements)
        bad = copy.deepcopy(good)
        bad["consumer"]["sourceBuilt"] = True
        bad["consumer"]["kind"] = "source-built"
        accepted, accepted_row = self._execute(active, good)
        rejected, rejected_row = self._execute(active, bad)
        self.assertEqual(accepted_row["status"], "pass")
        self.assertIn("142/142", accepted_row["detail"])
        self.assertEqual(accepted["status"], "blocked")
        self.assertEqual(rejected_row["status"], "fail")
        self.assertIn("source-built", rejected_row["detail"])
        self.assertEqual(rejected["status"], "fail")
        # A matching receipt while the matrix still says blocked must not pass silently.
        stale, stale_row = self._execute(committed, good)
        self.assertEqual(stale_row["status"], "fail")
        self.assertIn("set it active in matrix.json", stale_row["detail"])
        self.assertEqual(stale["status"], "fail")


if __name__ == "__main__":
    unittest.main()
