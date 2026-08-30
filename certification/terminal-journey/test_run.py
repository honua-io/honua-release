"""Self-tests for the deterministic terminal journey driver.

These run without a live stack, without Docker and without network access. They
guard the two things that make this lane trustworthy: the fail-closed roster
partition, and the stage-outcome discipline that stops a blocked journey from
ever reading as a pass.
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

spec = importlib.util.spec_from_file_location("terminal_gate", HERE / "run.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

import pins  # noqa: E402
import probes  # noqa: E402
import stages as stagelib  # noqa: E402

JOURNEY = json.loads((HERE / "journey.v1.json").read_text())
SCHEMA = json.loads((HERE / "receipt.schema.json").read_text())
POLICY = json.loads((HERE / "control-plane-roster.v1.json").read_text())
PROTOCOL = json.loads((HERE.parent / "terminal-model-canary" / "driver-protocol.v1.json").read_text())

MANIFEST = {
    "platformRelease": "2026.1-rc.2",
    "components": {
        "honua-server": {
            "sha": "a" * 40,
            "image": "ghcr.io/honua-io/honua-server:test",
            "digest": "sha256:" + "b" * 64,
        }
    },
    "clientArtifacts": {
        "honua-sdk-js": {
            "package": "@honua/sdk-js",
            "version": "0.0.0",
            "integrity": "sha512-x",
            "sourceSha": "c" * 40,
            "ecosystem": "npm",
            "publicationState": "published",
        },
        "honua-mcp-server": {
            "package": "@honua/mcp-server",
            "version": "0.0.0",
            "integrity": "sha512-y",
            "sourceSha": "d" * 40,
            "ecosystem": "npm",
            "publicationState": "published",
        },
    },
}


def build(**overrides):
    kwargs = dict(
        manifest=MANIFEST,
        journey=JOURNEY,
        roster=gate.roster_verdict(POLICY, None, None),
        evidence_uri="test://build",
        mode="build",
        target=None,
        target_path=None,
        workspace=pins.ClientWorkspace(status="blocked", root=None, reason="test"),
        stage_results=None,
        notices=[],
    )
    kwargs.update(overrides)
    return gate.build_receipt(**kwargs)


def validate(receipt):
    import jsonschema

    jsonschema.validate(receipt, SCHEMA)


# ---------------------------------------------------------------------------
# Control-plane roster partition
# ---------------------------------------------------------------------------
class RosterTests(unittest.TestCase):
    def test_policy_names_exactly_eleven_audited_exclusions(self):
        gate.validate_policy(POLICY)

    def test_missing_upstream_rosters_is_blocked_not_pass(self):
        self.assertEqual(gate.roster_verdict(POLICY, None, None)["status"], "blocked")

    def test_exact_partition_passes(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in POLICY["exclusions"]]
        rest = {"operationIds": projected + excluded}
        mcp = {"projectedOperationIds": projected, "exclusions": excluded}
        self.assertEqual(gate.roster_verdict(POLICY, rest, mcp)["status"], "pass")

    def test_duplicate_or_missing_operation_fails(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in POLICY["exclusions"]]
        rest = {"operationIds": projected + excluded}
        mcp = {"projectedOperationIds": projected[:-1] + [projected[0]], "exclusions": excluded}
        verdict = gate.roster_verdict(POLICY, rest, mcp)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(verdict["problems"])

    def test_roster_failure_overrides_blocked_stages_in_receipt(self):
        receipt = build(roster={"status": "fail", "problems": ["drift"]})
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure"]["check"], "control-plane-roster")

    def test_same_size_partition_with_wrong_exclusion_fails_and_names_drift(self):
        projected = [f"op-{i:03}" for i in range(385)]
        excluded = [row["id"] for row in POLICY["exclusions"]]
        swapped_secret, swapped_projection = excluded[0], projected[0]
        rest = {"operationIds": projected + excluded}
        mcp = {
            "projectedOperationIds": projected[1:] + [swapped_secret],
            "exclusions": excluded[1:] + [swapped_projection],
        }
        verdict = gate.roster_verdict(POLICY, rest, mcp)
        self.assertEqual(verdict["status"], "fail")
        self.assertTrue(any(swapped_secret in p for p in verdict["problems"]))
        self.assertTrue(any(swapped_projection in p for p in verdict["problems"]))


# ---------------------------------------------------------------------------
# Journey contract
# ---------------------------------------------------------------------------
class JourneyContractTests(unittest.TestCase):
    def test_every_stage_is_numbered_and_attributed(self):
        self.assertEqual([s["number"] for s in JOURNEY["stages"]], list(range(1, 9)))
        self.assertTrue(all(s["blockedBy"] for s in JOURNEY["stages"]))

    def test_every_stage_has_a_registered_implementation(self):
        for stage in JOURNEY["stages"]:
            self.assertIn(stage["number"], stagelib.STAGE_IMPLEMENTATIONS)

    def test_client_commands_are_known_required_commands(self):
        known = {c.command for c in pins.REQUIRED_COMMANDS}
        for stage in JOURNEY["stages"]:
            for command in stage["clientCommands"]:
                self.assertIn(command, known)

    def test_required_command_stage_mapping_matches_the_journey(self):
        for required in pins.REQUIRED_COMMANDS:
            declared = {
                s["number"] for s in JOURNEY["stages"] if required.command in s["clientCommands"]
            }
            self.assertEqual(
                declared,
                set(required.required_by),
                f"{required.command} stage mapping drifted between pins.py and journey.v1.json",
            )


# ---------------------------------------------------------------------------
# Stage-outcome discipline — the core honesty rules
# ---------------------------------------------------------------------------
class StageDisciplineTests(unittest.TestCase):
    def _no_blockers(self, number):
        return []

    def test_there_is_no_skip_state(self):
        stage_status = SCHEMA["$defs"]["stage"]["properties"]["status"]["enum"]
        self.assertEqual(sorted(stage_status), ["blocked", "fail", "pass"])
        self.assertNotIn("skip", stage_status)
        self.assertNotIn("skipped", stage_status)

    def test_all_eight_stages_are_always_materialized(self):
        results = stagelib.run_stages(JOURNEY, stagelib.Observation(), self._no_blockers)
        self.assertEqual([r.number for r in results], list(range(1, 9)))

    def test_a_blocked_stage_always_names_a_dependency(self):
        results = stagelib.run_stages(JOURNEY, stagelib.Observation(), self._no_blockers)
        for result in results:
            if result.status == "blocked":
                self.assertTrue(result.blocked_by, f"stage {result.number} is blocked with no dependency named")

    def test_an_unreachable_target_can_never_produce_a_pass(self):
        """Nothing observed means nothing passes."""
        results = stagelib.run_stages(JOURNEY, stagelib.Observation(), self._no_blockers)
        self.assertTrue(all(r.status != "pass" for r in results))

    def test_a_failing_check_makes_the_stage_fail_not_blocked(self):
        checks = [
            probes.Check("x.1", "http", "GET /healthz/ready", "fail", "server said no"),
            probes.blocked("x.2", "cli", "honua admin", "absent", ["ticket"]),
        ]
        result = stagelib._resolve(checks, 3, "publish-service", "cmd")
        self.assertEqual(result.status, "fail")

    def test_missing_client_command_blocks_exactly_the_declared_stages(self):
        workspace = pins.ClientWorkspace(
            status="pass",
            root=None,
            reason=None,
            command_surface=[
                {
                    "command": "honua admin",
                    "requiredBy": [2, 3, 8],
                    "status": "absent",
                    "providedBy": None,
                    "detail": "not shipped",
                }
            ],
        )
        self.assertTrue(workspace.missing_for_stage(2))
        self.assertTrue(workspace.missing_for_stage(3))
        self.assertTrue(workspace.missing_for_stage(8))
        self.assertFalse(workspace.missing_for_stage(4))

    def test_candidate_identity_must_match_manifest_revision(self):
        observation = stagelib.Observation(
            image_ref="candidate", expected_revision="expected",
            capability_manifest={"server": {"deploymentRevision": "different"}},
        )
        identity = next(c for c in stagelib.stage_1(observation, self._no_blockers) if c.id == "1.3-candidate-identity")
        self.assertEqual(identity.status, "fail")

    def test_api_key_check_uses_its_independent_observation(self):
        observation = stagelib.Observation(anonymous_admin_status=401, anonymous_api_keys_status=404)
        check = next(c for c in stagelib.stage_2(observation, self._no_blockers) if c.id == "2.2-admin-endpoint-present")
        self.assertEqual(check.status, "fail")

    def test_stage_8_honestly_names_server_3599(self):
        result = stagelib._resolve(stagelib.stage_8(stagelib.Observation(), self._no_blockers), 8, "approval", "approve")
        self.assertIn("https://github.com/honua-io/honua-server/issues/3599", result.blocked_by)

    def test_blocked_check_without_a_dependency_is_rejected_by_the_schema(self):
        receipt = build()
        receipt["stages"][0]["checks"] = [
            {"id": "x", "kind": "http", "invocation": "GET /", "status": "blocked", "detail": "", "blockedBy": []}
        ]
        with self.assertRaises(Exception):
            validate(receipt)


# ---------------------------------------------------------------------------
# Receipt schema
# ---------------------------------------------------------------------------
class ReceiptTests(unittest.TestCase):
    def test_build_receipt_validates_and_is_blocked(self):
        receipt = build()
        validate(receipt)
        self.assertEqual(receipt["mode"], "build")
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(len(receipt["stages"]), 8)
        self.assertTrue(all(s["status"] == "blocked" and s["blockedBy"] for s in receipt["stages"]))

    def test_build_mode_cannot_claim_pass(self):
        receipt = build()
        receipt["status"] = "pass"
        for stage in receipt["stages"]:
            stage["status"] = "pass"
            stage["blockedBy"] = []
        with self.assertRaises(Exception):
            validate(receipt)

    def test_harness_build_evidence_can_never_be_current_and_complete(self):
        receipt = build()
        receipt["stages"][0]["evidence"]["freshness"] = "verified-current"
        receipt["stages"][0]["evidence"]["completeness"] = "complete"
        with self.assertRaises(Exception):
            validate(receipt)

    def test_a_passing_stage_needs_a_live_observation(self):
        receipt = build()
        receipt["mode"] = "live"
        receipt["target"] = {
            "id": "local-docker",
            "kind": "local-docker",
            "configPath": "certification/terminal-journey/targets/local-docker.json",
            "configSha256": "e" * 64,
            "baseUrl": None,
            "composeProject": "p",
        }
        stage = receipt["stages"][0]
        stage["status"] = "pass"
        stage["blockedBy"] = []
        stage["checks"] = [
            {"id": "1.2", "kind": "http", "invocation": "GET /healthz/ready", "status": "pass", "detail": "Ready"}
        ]
        stage["evidence"] = {
            "uri": "u",
            "source": "harness-build",  # a build is not an observation
            "freshness": "verified-current",
            "completeness": "complete",
            "observedAt": None,
        }
        with self.assertRaises(Exception):
            validate(receipt)

    def test_client_artifact_block_matches_the_canary_equality_check(self):
        """The canary compares this block by exact equality; extra keys break it."""
        receipt = build()
        for pin in receipt["clientArtifacts"].values():
            self.assertEqual(
                sorted(pin), ["digest", "integrity", "package", "sourceSha", "version"]
            )

    def test_a_failing_receipt_names_the_numbered_stage_and_command(self):
        observation = stagelib.Observation(ready=False, readiness_detail="unreachable")
        results = stagelib.run_stages(JOURNEY, observation, lambda n: [])
        results[0].status = "fail"
        results[0].checks = [
            probes.Check("1.2-readiness", "http", "GET /healthz/ready", "fail", "unreachable")
        ]
        receipt = build(
            mode="live",
            target=json.loads((HERE / "targets" / "local-docker.json").read_text()),
            target_path=HERE / "targets" / "local-docker.json",
            workspace=pins.ClientWorkspace(
                status="pass",
                root=None,
                reason=None,
                resolved=[
                    pins.ResolvedArtifact(
                        name="honua-sdk-js",
                        package="@honua/sdk-js",
                        version="0.0.0",
                        ecosystem="npm",
                        registry_url=None,
                        integrity_verified=True,
                        tarball_sha256="f" * 64,
                        bin={"honua": "./bin.js"},
                    )
                ],
            ),
            stage_results=results,
        )
        self.assertEqual(receipt["status"], "fail")
        self.assertEqual(receipt["failure"]["number"], 1)
        self.assertEqual(receipt["failure"]["check"], "1.2-readiness")
        self.assertIn("readiness", receipt["failure"]["command"] + receipt["failure"]["check"])
        validate(receipt)

    def test_passing_mutation_stage_requires_canonical_ids(self):
        receipt = build()
        receipt["mode"] = "live"
        stage = receipt["stages"][2]
        stage.update(status="pass", blockedBy=[], checks=[{"id": "x", "kind": "mcp-tool", "invocation": "x", "status": "pass", "detail": "x"}])
        stage["evidence"] = {"uri": "u", "source": "live-local-docker", "freshness": "verified-current", "completeness": "complete", "observedAt": "2026-08-29T00:00:00Z"}
        with self.assertRaises(Exception):
            validate(receipt)


# ---------------------------------------------------------------------------
# Canary adapter contract
# ---------------------------------------------------------------------------
class DriverProtocolTests(unittest.TestCase):
    def test_adapter_lives_at_the_contracted_path(self):
        self.assertEqual(
            PROTOCOL["adapterPath"], "certification/terminal-journey/live_driver.py"
        )
        self.assertTrue((HERE / "live_driver.py").is_file())

    def test_every_protocol_operation_is_implemented(self):
        import live_driver

        for operation in PROTOCOL["operations"]:
            self.assertIn(operation, live_driver.OPERATIONS)

    def test_unknown_protocol_is_refused(self):
        import live_driver

        response = live_driver.handle({"protocol": "something-else", "operation": "setup"})
        self.assertEqual(response["status"], "fail")

    def test_unknown_operation_is_refused(self):
        import live_driver

        response = live_driver.handle({"protocol": live_driver.PROTOCOL, "operation": "nope"})
        self.assertEqual(response["status"], "fail")

    def test_execute_refuses_actions_outside_a_bounded_tool_view(self):
        """Protocol prohibition 2, asserted without a live stack."""
        import live_driver

        observation = stagelib.Observation(tool_names=("honua_render_map",))
        view = live_driver._tool_view(observation)
        self.assertFalse(view["bounded"])
        self.assertEqual(view["tools"], [])
        self.assertTrue(view["blockedBy"])

    def test_credential_references_carry_no_values(self):
        target = json.loads((HERE / "targets" / "local-docker.json").read_text())
        self.assertIn("env", target["adminPassword"])

    @mock.patch.dict("os.environ", {"HONUA_ADMIN_PASSWORD": "configured"})
    def test_configured_admin_password_wins_over_default(self):
        self.assertEqual(probes.resolve_env_default("HONUA_ADMIN_PASSWORD", "default"), "configured")

    def test_rehydrated_workspace_reuses_setup_metadata(self):
        original = pins.ClientWorkspace(status="pass", root=Path("clients"), reason=None, command_surface=[{"command": "honua", "requiredBy": [1], "status": "present"}])
        restored = pins.ClientWorkspace.from_receipt(original.as_receipt(), Path("clients"))
        self.assertEqual(restored.command_surface, original.command_surface)


class ProbeTests(unittest.TestCase):
    @mock.patch.object(probes, "http_get")
    @mock.patch.object(probes.time, "sleep")
    def test_negative_readiness_text_is_rejected(self, _sleep, http_get):
        http_get.return_value = probes.HttpResult(200, b"Not ready", "text/plain")
        ready, _detail = probes.wait_for_ready("http://example/ready", timeout_seconds=0)
        self.assertFalse(ready)

    @mock.patch.object(probes, "_enumerate_with")
    def test_broken_installed_proxy_remains_an_error(self, enumerate_with):
        enumerate_with.side_effect = [((), "exited silently"), (("tool",), None)]
        with mock.patch.object(Path, "resolve", return_value=Path("/real/proxy.js")):
            names, error, note = probes.enumerate_tools(Path("/shim/proxy"), "http://example/mcp")
        self.assertEqual(names, ("tool",))
        self.assertIn("installed executable", error)
        self.assertIsNotNone(note)


if __name__ == "__main__":
    unittest.main()
