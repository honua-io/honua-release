"""Self-tests for the deterministic terminal journey driver.

These run without a live stack, without Docker and without network access. They
guard the two things that make this lane trustworthy: the fail-closed roster
partition, and the stage-outcome discipline that stops a blocked journey from
ever reading as a pass.
"""
import importlib.util
import json
import os
import sys
import tempfile
import textwrap
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
        target_base_url=None,
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
                {"command": "honua-mcp-proxy", "status": "present", "requiredBy": [1, 4, 5, 6, 7]},
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

    def test_present_admin_does_not_claim_credential_or_mutation_execution(self):
        workspace = pins.ClientWorkspace(status="pass", root=None, reason=None,
            command_surface=[{"command": "honua admin", "status": "present"}])
        observation = stagelib.Observation(anonymous_api_keys_status=401)
        for number in (2, 3, 8):
            with self.subTest(stage=number):
                checks = stagelib.STAGE_IMPLEMENTATIONS[number](observation, workspace.missing_for_stage)
                discovery = next(c for c in checks if c.id == f"{number}.1-admin-cli")
                self.assertEqual(discovery.status, "pass")
                result = stagelib._resolve(checks, number, "test", "test")
                self.assertEqual(result.status, "blocked")
                self.assertIn(stagelib.JOURNEY_DRIVER, result.blocked_by)
                self.assertNotIn(stagelib.INSTALLED_CLIENTS, result.blocked_by)
                self.assertFalse(any("unshipped" in c.detail or "not available on the candidate" in c.detail for c in checks))

    def test_incomplete_or_ambiguous_command_evidence_blocks(self):
        for rows in ([], [{"command": "honua admin", "status": "unknown"}],
                     [{"command": "honua admin", "status": "present"}] * 2):
            with self.subTest(rows=rows):
                workspace = pins.ClientWorkspace(status="pass", root=None, reason=None, command_surface=rows)
                for number in (2, 3, 8):
                    self.assertTrue(workspace.missing_for_stage(number))
        workspace = pins.ClientWorkspace(status="blocked", root=None, reason="integrity mismatch",
            command_surface=[{"command": "honua admin", "status": "present"}])
        self.assertEqual(workspace.missing_for_stage(2), ["integrity mismatch"])

    def test_failed_help_cannot_certify_admin_even_if_it_prints_the_verb(self):
        # Real child process: fixed fixture advertises admin then exits nonzero.
        # The expected result follows from its exit code, not a captured receipt.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli = root / "cli.js"
            artifact = pins.ResolvedArtifact("test", "test", "0.0.0", "npm", None,
                                             True, "a" * 64, {"honua": "cli.js"}, root=root)
            for code, expected in ((0, True), (2, False)):
                with self.subTest(exit_code=code):
                    cli.write_text(f'console.log("  admin  Admin commands"); process.exit({code});')
                    present, detail = pins._honua_has_admin(artifact, "cli.js")
                    self.assertEqual(present, expected, detail)

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

    def test_live_receipt_records_effective_target_url(self):
        receipt = build(
            mode="live",
            target=json.loads((HERE / "targets" / "local-docker.json").read_text()),
            target_path=HERE / "targets" / "local-docker.json",
            target_base_url="http://127.0.0.1:8137",
        )
        self.assertEqual(receipt["target"]["baseUrl"], "http://127.0.0.1:8137")
        validate(receipt)

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

    def _passing_stage(self, number):
        receipt = build()
        receipt["mode"] = "live"
        stage = receipt["stages"][number - 1]
        stage.update(status="pass", blockedBy=[], checks=[{"id": "x", "kind": "mcp-tool", "invocation": "x", "status": "pass", "detail": "x"}])
        stage["evidence"] = {"uri": "u", "source": "live-local-docker", "freshness": "verified-current", "completeness": "complete", "observedAt": "2026-08-29T00:00:00Z"}
        stage.update(operationId="studio.draft.save-version", operationInstanceId="opinst-1",
                     correlationId="00-corr", auditId="audit-1")
        return receipt, stage

    def test_passing_mutation_stage_is_keyed_on_server_emitted_ids_not_legacy_ids(self):
        # The candidate emits operationInstanceId/correlationId/auditId; policy-decision,
        # actuator, verification and approval ids are optional and may stay null.
        receipt, stage = self._passing_stage(6)
        self.assertIsNone(stage["policyDecisionId"])
        self.assertIsNone(stage["actuatorId"])
        self.assertIsNone(stage["verificationId"])
        validate(receipt)
        for key in ("policyDecisionId", "approvalId", "actuatorId", "verificationId"):
            del stage[key]
        validate(receipt)
        for key in ("operationInstanceId", "correlationId", "auditId"):
            with self.subTest(missing=key):
                changed, row = self._passing_stage(6)
                row[key] = None
                with self.assertRaises(Exception):
                    validate(changed)

    def test_passing_proposal_and_approval_stages_require_the_server_proposal_id(self):
        for number in (7, 8):
            with self.subTest(stage=number):
                receipt, stage = self._passing_stage(number)
                with self.assertRaises(Exception):
                    validate(receipt)
                stage["proposalId"] = "proposal-1"
                self.assertIsNone(stage["approvalId"])
                validate(receipt)

    def test_contract_marks_legacy_identities_optional(self):
        legacy = {"policyDecisionId", "approvalId", "actuatorId", "verificationId"}
        self.assertFalse(legacy & set(JOURNEY["receiptRequired"]))
        self.assertEqual(legacy, set(JOURNEY["receiptOptional"]))
        self.assertTrue({"operationInstanceId", "correlationId", "auditId", "proposalId"} <= set(JOURNEY["receiptRequired"]))
        closed = {"https://github.com/honua-io/honua-server/issues/3411",
                  "https://github.com/honua-io/honua-server/issues/3431",
                  "https://github.com/honua-io/honua-server/issues/3741"}
        self.assertFalse(closed & {b for s in JOURNEY["stages"] for b in s["blockedBy"]})


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


class CredentialPreflightTests(unittest.TestCase):
    ADMIN_KEY = "root-admin-secret-value"
    ISSUED = "issued-one-time-material"
    KEY_ID = "11111111-1111-4111-8111-111111111111"

    def _write_honua(self, directory: Path, mode: str) -> Path:
        script = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json, os, stat, sys
            from pathlib import Path
            MODE = {mode!r}
            ADMIN = {self.ADMIN_KEY!r}
            ISSUED = {self.ISSUED!r}
            KEY_ID = {self.KEY_ID!r}
            NAME = "honua-terminal-journey-preflight"
            argv = sys.argv[1:]
            if not argv or argv[0] != "admin":
                sys.stderr.write("Unknown command: " + (argv[0] if argv else "") + "\\n")
                raise SystemExit(2)
            config = Path(os.environ["HONUA_CONFIG_HOME"])
            config.mkdir(parents=True, exist_ok=True)
            log_path = config / "argv-log.json"
            previous = json.loads(log_path.read_text()) if log_path.exists() else []
            previous.append({{
                "argv": argv,
                "home": os.environ.get("HOME"),
                "configHome": os.environ.get("HONUA_CONFIG_HOME"),
                "adminKeyInArgv": any(ADMIN and ADMIN in arg for arg in argv),
                "issuedInArgv": any(ISSUED in arg for arg in argv),
            }})
            log_path.write_text(json.dumps(previous))
            if os.environ.get("HONUA_ADMIN_KEY") != ADMIN:
                sys.stderr.write("missing admin credential\\n")
                raise SystemExit(2)

            def flag(name):
                if name not in argv:
                    return None
                index = argv.index(name)
                return argv[index + 1] if index + 1 < len(argv) else None

            operation = argv[argv.index("secure") + 1]
            state_path = config / "state.json"
            state = json.loads(state_path.read_text()) if state_path.exists() else {{"revoked": False}}

            def emit(document):
                sys.stdout.write(json.dumps(document) + "\\n")
                if MODE == "leak" and operation == "createAdminApiKey":
                    sys.stdout.write(ADMIN + "\\n" + ISSUED + "\\n")

            if operation == "createAdminApiKey":
                body = json.loads(flag("--body") or "{{}}")
                if body.get("name") != NAME or body.get("permissions") != ["admin:read"]:
                    raise SystemExit(2)
                sink = Path(flag("--secret-output"))
                sink.write_text(ISSUED, encoding="utf-8")
                os.chmod(sink, 0o644 if MODE == "loose" else 0o600)
                emit({{
                    "operationId": "createAdminApiKey",
                    "resource": {{
                        "id": KEY_ID,
                        "name": NAME,
                        "permissions": ["admin:read"],
                        "status": "active",
                        "keyPrefix": "hnua_pre",
                    }},
                    "secretWritten": True,
                    "secretOutput": str(sink),
                    "secretSha256": "ab" * 32,
                }})
            elif operation == "getAdminApiKeyEffectivePermissions":
                grants = ["admin:read", "admin:write"] if MODE == "broad" else ["admin:read"]
                emit({{
                    "success": True,
                    "data": {{
                        "id": KEY_ID,
                        "name": NAME,
                        "status": "active",
                        "permissions": grants,
                        "canAuthenticate": True,
                    }},
                }})
            elif operation == "listAdminApiKeys":
                status = "revoked" if state.get("revoked") else "active"
                emit({{
                    "success": True,
                    "data": [{{
                        "id": KEY_ID,
                        "name": NAME,
                        "status": status,
                        "permissions": ["admin:read"],
                        "keyPrefix": "hnua_pre",
                    }}],
                }})
            elif operation == "revokeAdminApiKey":
                if MODE == "revoke-fails":
                    sys.stderr.write("revoke refused\\n")
                    raise SystemExit(1)
                state["revoked"] = True
                state_path.write_text(json.dumps(state))
                emit({{"success": True, "data": {{"id": KEY_ID, "status": "revoked", "name": NAME}}}})
            else:
                raise SystemExit(2)
            """
        )
        path = directory / "honua"
        path.write_text(script)
        path.chmod(0o755)
        return path

    def _run(self, mode: str, base_url: str = "http://127.0.0.1:8137"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        workdir = Path(tmp.name)
        honua = self._write_honua(workdir, mode)
        probe = probes.run_credential_preflight(
            honua=honua,
            base_url=base_url,
            admin_key=self.ADMIN_KEY,
            workdir=workdir / "probe",
        )
        log = json.loads((workdir / "probe" / "config" / "argv-log.json").read_text())
        return probe, log, workdir / "probe"

    def test_preflight_revokes_and_omits_credential_material(self):
        probe, log, probe_dir = self._run("pass")
        self.assertEqual(probe.status, "pass", probe.detail)
        self.assertEqual(probe.key_id, self.KEY_ID)
        self.assertNotIn(self.ADMIN_KEY, probe.detail)
        self.assertNotIn(self.ISSUED, probe.detail)
        self.assertFalse((probe_dir / "one-time-secret").exists())
        self.assertIn("revokeAdminApiKey", " ".join(" ".join(row["argv"]) for row in log))
        self.assertTrue(all(row["adminKeyInArgv"] is False and row["issuedInArgv"] is False for row in log))
        self.assertTrue(all(row["home"] == str(probe_dir) for row in log))
        self.assertTrue(all(row["configHome"] == str(probe_dir / "config") for row in log))
        self.assertNotIn(self.ADMIN_KEY, json.dumps(log))
        self.assertNotIn(self.ISSUED, json.dumps(log))

    def test_leaked_credential_fails_closed_and_is_redacted(self):
        probe, _log, probe_dir = self._run("leak")
        self.assertEqual(probe.status, "fail")
        self.assertNotIn(self.ADMIN_KEY, probe.detail)
        self.assertNotIn(self.ISSUED, probe.detail)
        self.assertIn("redacted", probe.detail)
        self.assertFalse((probe_dir / "one-time-secret").exists())

    def test_broader_effective_grants_fail(self):
        probe, log, _probe_dir = self._run("broad")
        self.assertEqual(probe.status, "fail")
        self.assertIn("effective permissions", probe.detail)
        self.assertIn("revokeAdminApiKey", " ".join(" ".join(row["argv"]) for row in log))

    def test_loose_sink_mode_fails(self):
        probe, _log, probe_dir = self._run("loose")
        self.assertEqual(probe.status, "fail")
        self.assertIn("0600", probe.detail)
        self.assertFalse((probe_dir / "one-time-secret").exists())

    def test_revoke_failure_is_not_a_pass(self):
        probe, _log, probe_dir = self._run("revoke-fails")
        self.assertEqual(probe.status, "fail")
        self.assertIn("revokeAdminApiKey", probe.detail)
        self.assertFalse((probe_dir / "one-time-secret").exists())

    def test_non_loopback_http_does_not_start_the_cli(self):
        probe = probes.run_credential_preflight(
            honua=Path("/does/not/exist"),
            base_url="http://example.com",
            admin_key=self.ADMIN_KEY,
            workdir=Path("/tmp/unused-credential-preflight"),
        )
        self.assertEqual(probe.status, "fail")
        self.assertIn("non-loopback", probe.detail)

    def test_passing_preflight_does_not_pass_later_stages_or_the_journey(self):
        probe = probes.CredentialProbe(
            status="pass",
            detail="temporary admin:read key checked; private sink deleted",
            key_id=self.KEY_ID,
        )
        observation = stagelib.Observation(
            image_ref="candidate",
            ready=True,
            readiness_detail="Ready",
            licensing_disabled=True,
            licensing_detail="admin license mode: disabled",
            capability_manifest={
                "server": {"deploymentRevision": "a" * 40, "deploymentRevisionSource": "commit-sha"}
            },
            expected_revision="a" * 40,
            anonymous_admin_status=401,
            anonymous_api_keys_status=401,
            tools_error="proxy unavailable",
            credential_probe=probe,
        )
        results = stagelib.run_stages(JOURNEY, observation, lambda _number: [])
        self.assertEqual(results[1].status, "pass")
        self.assertTrue(all(result.status != "pass" for index, result in enumerate(results) if index != 1))
        self.assertNotIn(self.ADMIN_KEY, json.dumps([check.as_receipt() for check in results[1].checks]))
        receipt = build(
            mode="live",
            target=json.loads((HERE / "targets" / "local-docker.json").read_text()),
            target_path=HERE / "targets" / "local-docker.json",
            target_base_url="http://127.0.0.1:8137",
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
                command_surface=[{"command": "honua admin", "requiredBy": [2, 3, 8], "status": "present", "providedBy": "@honua/sdk-js"}],
            ),
            stage_results=results,
        )
        validate(receipt)
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["stages"][1]["status"], "pass")
        self.assertTrue(all(stage["status"] != "pass" for stage in receipt["stages"] if stage["number"] != 2))

    def test_observe_runs_preflight_only_after_readiness(self):
        target = json.loads((HERE / "targets" / "local-docker.json").read_text())
        workspace = pins.ClientWorkspace(status="pass", root=None, reason=None)
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            honua = bindir / "honua"
            honua.write_text("#!/bin/sh\nexit 1\n")
            honua.chmod(0o755)
            blocked = probes.CredentialProbe(status="blocked", detail="not sent", blocked_by=["ticket"])
            with mock.patch.object(probes, "wait_for_ready", return_value=(False, "down")), mock.patch.object(
                probes, "run_credential_preflight"
            ) as preflight:
                early = gate.observe(target, "http://127.0.0.1:8137", workspace, bindir, None, "a" * 40)
            preflight.assert_not_called()
            self.assertIsNone(early.credential_probe)

            with mock.patch.object(probes, "wait_for_ready", return_value=(True, "Ready")), mock.patch.object(
                probes, "http_get", return_value=probes.HttpResult(401, b"", "text/plain")
            ), mock.patch.object(probes, "enumerate_tools", return_value=((), "proxy down", None)), mock.patch.object(
                gate, "assert_disabled"
            ), mock.patch.dict(
                os.environ, {"HONUA_ADMIN_PASSWORD": self.ADMIN_KEY}
            ), mock.patch.object(
                probes, "run_credential_preflight", return_value=blocked
            ) as preflight:
                observed = gate.observe(target, "http://127.0.0.1:8137", workspace, bindir, "candidate", "a" * 40)
            preflight.assert_called_once()
            self.assertEqual(preflight.call_args.kwargs["admin_key"], self.ADMIN_KEY)
            self.assertEqual(preflight.call_args.kwargs["base_url"], "http://127.0.0.1:8137")
            self.assertFalse(preflight.call_args.kwargs["workdir"].exists())
            self.assertIs(observed.credential_probe, blocked)
            self.assertNotIn(self.ADMIN_KEY, json.dumps(observed.credential_probe.__dict__))


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


class NpmInstallEnvironmentTests(unittest.TestCase):
    def test_pinned_install_ignores_scripts_and_drops_cloud_credentials(self):
        import subprocess

        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["env"] = dict(kwargs["env"])
            return subprocess.CompletedProcess(argv, 0, "", "")

        stripped = {
            "AWS_SECRET_ACCESS_KEY": "aws-secret",
            "AWS_SESSION_TOKEN": "aws-session",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "oidc-token",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://oidc.example/token",
            "GITHUB_TOKEN": "ghs_example",
            "GH_TOKEN": "gh_example",
            "HONUA_AWS_ROLE_ARN": "arn:aws:iam::123456789012:role/release",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pins.subprocess, "run", fake_run), mock.patch.dict(
            os.environ, {**stripped, "PATH": "/usr/bin"}, clear=False
        ):
            result = pins._npm_install(Path(tmp), ["sdk.tgz"], [])
        self.assertEqual(result.returncode, 0)
        self.assertIn("--ignore-scripts", captured["argv"])
        self.assertEqual(pins.EXPLICIT_LIFECYCLE_SCRIPTS, ())
        for key in captured["env"]:
            self.assertFalse(key.startswith(("AWS_", "ACTIONS_ID_TOKEN_REQUEST_", "HONUA_AWS_")))
            self.assertNotIn(key, {"GITHUB_TOKEN", "GH_TOKEN"})
        self.assertEqual(captured["env"]["PATH"], "/usr/bin")
        for value in stripped.values():
            self.assertNotIn(value, captured["env"].values())


if __name__ == "__main__":
    unittest.main()
