"""Trust-boundary contracts for release workflow triggers and gates."""
import re
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_contract_suppression as ccs  # noqa: E402
import check_release_promotion_approval as cpa  # noqa: E402


def _workflow(name: str) -> dict:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    return workflow


def _triggers(workflow: dict) -> dict:
    # PyYAML 1.1 parses the unquoted Actions key `on` as boolean true.
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict)
    return triggers


def test_manifest_validate_runs_for_every_pull_request_with_stable_check_name():
    workflow = _workflow("manifest-validate.yml")
    pull_request = _triggers(workflow).get("pull_request")
    assert pull_request is None or "paths" not in pull_request
    assert "validate" in workflow["jobs"]


def test_capacity_soak_consumes_the_frozen_lock_and_cannot_neutralize_failure():
    workflow = _workflow("capacity-soak.yml")
    job = workflow["jobs"]["frozen-slo"]
    commands = "\n".join(_step_text(step) for step in job["steps"])
    assert "capacity-envelope.v1.json" in commands
    assert "check_capacity_soak.py" in commands
    assert 'gh attestation verify "$RUNNER_TEMP/soak-receipt.json" --repo honua-io/honua-server' in commands
    assert "continue-on-error" not in str(job)


def test_real_release_cut_verifies_published_bytes_and_producer_trust():
    freeze = _workflow("release-train.yml")["jobs"]["freeze"]
    commands = "\n".join(_step_text(step) for step in freeze["steps"])
    assert "--exact-candidate" in commands
    assert "verify_client_artifacts.py" in commands
    assert "verify_evidence_sources.py" in commands
    assert "generate_evidence_index.py" in commands
    assert 'DRY_RUN" = "false' in commands


def test_live_release_aggregate_fails_on_any_skipped_required_gate():
    report = _workflow("release-train.yml")["jobs"]["report"]
    commands = "\n".join(_step_text(step) for step in report["steps"])
    assert '($dry=="false" and any(.[]; .status=="skipped")) then "fail"' in commands
    assert "best-effort" not in commands
    assert not any(_neutralised(step) for step in report["steps"])


def test_required_cloud_cell_cannot_self_skip_on_real_cut():
    workflow = _workflow("e2e-cloud-aws.yml")
    run = next(step["run"] for step in workflow["jobs"]["iac-live"]["steps"] if step.get("id") == "iac")
    assert '[ "$REQUIRE_REAL" = "true" ]' in run
    assert 'STATUS=fail; WHY="required cloud certification evidence missing' in run


def test_fail_closed_demo_deliberately_skips_required_cell_and_goes_red():
    job = _workflow("manifest-validate.yml")["jobs"]["validate"]
    step = next(step for step in job["steps"] if str(step.get("name", "")).startswith("Acceptance demo"))
    commands = _step_text(step)
    assert "endsWith(github.ref_name, '-red-demo')" in step["if"]
    assert "validate_live_report" in commands
    assert '"status": "skipped" if gate == skipped else "pass"' in commands
    assert "raise SystemExit(1)" in commands
    assert not _neutralised(job)
    assert not _neutralised(step)


def test_artifact_gate_uses_client_pins_and_strict_mode_rejects_local_fallbacks():
    workflow = _workflow("gate-artifact-consume.yml")
    jobs = workflow["jobs"]
    resolve = "\n".join(_step_text(step) for step in jobs["resolve_pins"]["steps"])
    assert 'manifest.get("clientArtifacts")' in resolve
    assert "honua-mcp-server" in resolve
    assert "consume-mcp-npm" in jobs
    report = "\n".join(_step_text(step) for step in jobs["report"]["steps"])
    assert '.source=="local"' in report and 'enf=="strict"' in report


def test_conformance_gate_consumes_manifest_pinned_evidence_producers():
    jobs = _workflow("certification.yml")["jobs"]
    mcp = "\n".join(_step_text(step) for step in jobs["conformance-mcp"]["steps"])
    esri = "\n".join(_step_text(step) for step in jobs["conformance-esri-geoservices"]["steps"])
    cite = "\n".join(_step_text(step) for step in jobs["conformance-ogc-stac"]["steps"])
    assert 'get("evidenceSources")' in mcp and 'get("mcp")' in mcp
    assert "checkout_component.sh" in mcp and "clone --depth 1" not in mcp
    assert 'get("evidenceSources")' in esri and 'get("esri-compat")' in esri
    assert 'get("evidenceSources")' in cite and 'get("cite")' in cite
    assert "/trunk/docs/cite-status.md" not in cite


def test_manifest_validate_binds_python_snapshots_to_the_pinned_source():
    workflow = _workflow("manifest-validate.yml")
    text = "\n".join(
        _step_text(step) for step in workflow["jobs"]["validate"]["steps"]
    )
    assert '.sources["sdk-python"].commit' in text
    assert "honua-sdk-python/contents/compatibility/sdk-coverage.v1.json?ref=$sdk_python_sha" in text
    assert "honua-sdk-python/contents/conformance/protocol-certification.v1.json?ref=$sdk_python_sha" in text
    assert "cmp certification/sources/sdk-python/sdk-coverage.v1.json" in text
    assert "cmp certification/sources/sdk-python/protocol-certification.v1.json" in text


def test_manifest_validate_binds_server_and_dotnet_snapshots_to_pinned_sources():
    workflow = _workflow("manifest-validate.yml")
    text = "\n".join(
        _step_text(step) for step in workflow["jobs"]["validate"]["steps"]
    )
    assert ".sources.server.commit" in text
    assert "honua-server/contents/docs/gis/data/capability-matrix.v1.json?ref=$server_sha" in text
    assert "cmp certification/sources/server/capability-matrix.v1.json" in text
    assert '.sources["sdk-dotnet"].commit' in text
    assert "honua-sdk-dotnet/contents/contracts/sdk-coverage.v1.json?ref=$sdk_dotnet_sha" in text
    assert "honua-sdk-dotnet/contents/contracts/sdk-certification.v1.json?ref=$sdk_dotnet_sha" in text
    assert "cmp certification/sources/sdk-dotnet/sdk-coverage.v1.json" in text
    assert "cmp certification/sources/sdk-dotnet/sdk-certification.v1.json" in text


def test_protocol_certification_uses_the_ledger_owner_revision_not_the_run_sha():
    gate = (REPO_ROOT / ".github" / "workflows" / "gate-protocol-certification.yml").read_text(
        encoding="utf-8"
    )
    assert "inputs.expected_requirements_source_revision" in gate
    assert (
        "EXPECTED_REQUIREMENTS_REVISION: ${{ github.event.pull_request.head.sha || github.sha }}"
        not in gate
    )
    assert "EXPECTED_REQUIREMENTS_REVISION" in gate
    assert "out/requirements-owner" in gate
    assert "out/requirements-owner/certification/protocol-certification-requirements.v1.json" in gate
    assert "--requirements out/protocol-certification-requirements.v1.json" in gate

    for caller in ("pr-protocol-certification.yml", "nightly-protocol-certification.yml"):
        text = (REPO_ROOT / ".github" / "workflows" / caller).read_text(encoding="utf-8")
        assert "PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION" in text
        assert (
            "honua-io/honua-release/.github/workflows/gate-protocol-certification.yml@"
            in text
        )
        assert "uses: ./.github/workflows/gate-protocol-certification.yml" not in text

    release_train = (REPO_ROOT / ".github" / "workflows" / "release-train.yml").read_text(
        encoding="utf-8"
    )
    assert "ledger['requirementsSourceRevision']" in release_train
    assert "expected_requirements_source_revision:" in release_train


def test_release_protocol_gate_binds_all_shipped_producers_to_the_manifest():
    gate = (REPO_ROOT / ".github" / "workflows" / "gate-protocol-certification.yml").read_text(
        encoding="utf-8"
    )
    train = (REPO_ROOT / ".github" / "workflows" / "release-train.yml").read_text(
        encoding="utf-8"
    )
    assert "expected_server_certification_sha" in gate
    assert "--expected-server-certification-sha" in gate
    assert "serverCertificationProducerSha" in train
    assert "server_certification_sha" in train
    assert "expected_server_certification_sha:" in train
    for source in ("js", "python", "dotnet"):
        assert f"expected_sdk_{source}_sha" in gate
        assert f"--expected-sdk-{source}-sha" in gate
        assert f"sdk_{source}_sha" in train
        assert f"expected_sdk_{source}_sha:" in train
        assert f"expected_sdk_{source}_version" in gate
        assert f"--expected-sdk-{source}-version" in gate
        assert f"sdk_{source}_version" in train
        assert f"expected_sdk_{source}_version:" in train
    for source in ("grpc", "mcp"):
        assert f"expected_geospatial_{source}_sha" in gate
        assert f"--expected-geospatial-{source}-sha" in gate
        assert f"geospatial_{source}_sha" in train
        assert f"expected_geospatial_{source}_sha:" in train


def test_protocol_certification_uses_a_pinned_evaluator_and_honors_bootstrap_unavailability():
    gate = (REPO_ROOT / ".github" / "workflows" / "gate-protocol-certification.yml").read_text(
        encoding="utf-8"
    )

    assert "Test the proposed evaluator separately" in gate
    assert "out/requirements-owner/tools/check_protocol_certification.py" in gate
    assert "python3 tools/check_protocol_certification.py \"${args[@]}\"" not in gate
    assert 'if [[ "$ENFORCEMENT" == "bootstrap" && "$FETCH_CLASS" == "unavailable" ]]; then' in gate
    assert 'echo "fetch_class=$fetch_class" >> "$GITHUB_OUTPUT"' in gate
    assert 'fetch_class=integrity' in gate
    assert "if: steps.evaluate.outputs.status == 'fail'" in gate
    release_train = (REPO_ROOT / ".github" / "workflows" / "release-train.yml").read_text(
        encoding="utf-8"
    )
    assert (
        "honua-io/honua-release/.github/workflows/gate-protocol-certification.yml@"
        in release_train
    )
    assert "uses: ./.github/workflows/gate-protocol-certification.yml" not in release_train


def test_convergence_rebind_is_plan_only_by_default_and_creates_one_review_pr():
    text = (REPO_ROOT / ".github/workflows/convergence-rebind.yml").read_text(encoding="utf-8")
    assert "default: false" in text
    assert "python tools/convergence_rebind.py --apply" in text
    assert text.count("gh workflow run aggregate.yml") == 1
    assert text.count("gh pr create") == 1
    assert "--body-file rebind-receipt.md" in text
    assert "gh variable set" not in text
    assert "git commit --allow-empty" in text
    assert '--ref "$correlation_ref"' in text
    assert '--branch "$correlation_ref"' in text
    assert "git/refs/heads/$correlation_ref" in text


def test_convergence_activation_is_merge_bound_and_sets_all_three_variables():
    text = (REPO_ROOT / ".github/workflows/convergence-rebind-activate.yml").read_text(encoding="utf-8")
    assert "github.event.pull_request.merged == true" in text
    assert "github.event.pull_request.base.ref == github.event.repository.default_branch" in text
    assert "github.event.pull_request.head.repo.full_name == github.repository" in text
    assert "startsWith(github.event.pull_request.head.ref, 'convergence-rebind/')" in text
    assert "compare/${{ steps.binding.outputs.requirements_revision }}...${{ github.event.pull_request.merge_commit_sha }}" in text
    assert "use a merge commit and refuse activation" in text
    assert 'yaml.safe_load(open(path))["jobs"]' in text
    assert 'ledger["candidate"] == expected_candidate' in text
    assert 'ledger["requirements_source_revision"] == os.environ["REQUIREMENTS_REVISION"]' in text
    assert "Merged ledger digest mismatch" in text
    assert "trap rollback ERR" in text
    assert 'gh variable get "$name"' in text
    assert text.count('gh variable set "${names[$i]}"') == 2
    for name in (
        "PROTOCOL_CERTIFICATION_MATRIX_COMMIT",
        "PROTOCOL_CERTIFICATION_MATRIX_SHA256",
        "PROTOCOL_CERTIFICATION_REQUIREMENTS_SOURCE_REVISION",
    ):
        assert text.count(name) == 1


def _step_text(step: dict) -> str:
    """Everything a step can DO: its shell, the action it invokes, and that action's inputs.

    Scanning only `run:` was the flaw in the previous version of these contracts — a release performed
    by a `uses:` action, or a gate neutralised by `continue-on-error`, was invisible to them.
    """
    parts = [str(step.get("run", "")), str(step.get("uses", "")), str(step.get("with", ""))]
    return "\n".join(parts)


def _neutralised(node: dict) -> bool:
    """True when a job/step is allowed to fail without failing the workflow."""
    value = node.get("continue-on-error", False)
    if isinstance(value, str):
        # An expression (`${{ ... }}`) or the string "true" both mean "may be neutralised".
        return value.strip().lower() not in {"", "false"}
    return bool(value)


def _logical_lines(text: str) -> list[str]:
    """Shell lines with continuations joined, so one command is scanned as one unit.

    Scanning raw lines was an evasion: a `gh api` call split across `\\` continuations put its
    mutating flag on a following line, where the per-line check never looked for it.
    """
    lines: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1].rstrip() + " "
            continue
        joined = (buffer + stripped).strip()
        if joined:
            lines.append(joined)
        buffer = ""
    if buffer.strip():
        lines.append(buffer.strip())
    return lines


def _gh_api_arguments(line: str) -> str | None:
    """The argument tail of a `gh api` invocation, or None when the line is not one."""
    match = re.search(r"\bgh\s+api\b", line)
    return line[match.end():] if match else None


def assert_read_only(commands: str, where: str) -> None:
    """Fail if any shell command in `commands` can write through the GitHub API."""
    for line in _logical_lines(commands):
        arguments = _gh_api_arguments(line)
        if arguments is not None:
            for pattern, name in MUTATING_GH_API_FLAGS:
                assert not re.search(pattern, arguments), (
                    f"mutating gh api flag {name} in {where}: {line}"
                )
        for command in MUTATING_GH_COMMANDS:
            assert command not in line, f"mutating gh command in {where}: {line}"


# Anything that publishes, signs, tags, or finalises a release. None of it may run before, or
# independently of, the approval gate.
RELEASE_ACTIONS = (
    "finalize_release.py",
    "cosign",
    "sigstore/",
    "gh release create",
    "action-gh-release",
    "generate_bom.py",
)

# Every way `gh api` can be made to write. `-f`/`-F`/`--field`/`--raw-field` and `--input` switch gh
# to POST implicitly, so checking only `-X`/`--method` verbs is not enough.
#
# These are matched as PATTERNS against the argument tail that follows `gh api`, not as substrings of
# the whole line. Two evasions made the literal form insufficient:
#   * `-f `/`-F ` with a trailing space missed pflag's ATTACHED shorthand (`-fwait_timer=0`), and
#   * matching anywhere on the line would flag an innocent `[ -f file ]` test that happens to share a
#     line with a read-only call.
# Scanning the tail after `gh api` catches the attached form without the false positive.
MUTATING_GH_API_FLAGS = (
    (r"(?:^|\s)-X(?=\s|=|[A-Za-z])", "-X"),
    (r"(?:^|\s)--method\b", "--method"),
    (r"(?:^|\s)-[fF](?=\s|=|[A-Za-z0-9_])", "-f/-F (including the attached form)"),
    (r"(?:^|\s)--(?:raw-)?field\b", "--field/--raw-field"),
    (r"(?:^|\s)--input\b", "--input"),
)

# Subcommands that write regardless of flags.
MUTATING_GH_COMMANDS = (
    "gh secret",
    "gh variable",
    "gh release",
    "gh workflow run",
    "gh pr ",
    "gh issue ",
    "gh api graphql",
)


def test_promote_preflights_environment_policy_before_release_steps():
    workflow = _workflow("promote.yml")
    promote = workflow["jobs"]["promote"]
    assert promote["environment"] == "release-promotion"
    assert not _neutralised(promote), "the promote job must not be continue-on-error"

    steps = promote["steps"]
    texts = [_step_text(step) for step in steps]
    joined = "\n".join(texts)

    # The gate runs, from settings-as-code, in its enforcing mode, with the promotion identity.
    assert "tools/check_release_promotion_approval.py" in joined
    assert "certification/release-promotion-approval.yaml" in joined
    assert "--mode promote" in joined
    assert "--promotion-ref" in joined and "--promotion-actor" in joined

    approval_index = next(
        index for index, text in enumerate(texts) if "check_release_promotion_approval.py" in text
    )
    # A gate that is allowed to fail is not a gate.
    assert not _neutralised(steps[approval_index]), "the approval gate step must not be continue-on-error"
    # ...and neither is one that runs after the release has already happened.
    before = "\n".join(texts[:approval_index])
    for action in RELEASE_ACTIONS:
        assert action not in before, f"{action} runs before the approval gate"

    # Promotion identity is bound to the certified train run, and the policy/checkers are read from
    # the default branch rather than from whatever ref the promotion was dispatched on.
    assert "candidate_binding.py validate-run" in joined
    first_checkout = next(step for step in steps if "actions/checkout" in str(step.get("uses", "")))
    assert (first_checkout.get("with") or {}).get("ref") == "${{ github.event.repository.default_branch }}"


def test_every_promote_step_that_can_refuse_is_allowed_to_refuse():
    promote = _workflow("promote.yml")["jobs"]["promote"]
    for step in promote["steps"]:
        text = _step_text(step)
        if any(
            guard in text
            for guard in ("check_release_promotion_approval.py", "check_promotion_readiness.py",
                          "candidate_binding.py", "finalize_release.py")
        ):
            assert not _neutralised(step), f"guard step is neutralised: {step.get('name')}"


def test_promotion_requires_committed_burn_evidence_and_retags_the_freeze_rc():
    workflow = _workflow("promote.yml")
    assert workflow["jobs"]["promote"]["environment"] == "release-promotion"
    assert _triggers(workflow)["workflow_dispatch"]["inputs"]["promotion_record"]["required"] is True
    commands = "\n".join(_step_text(step) for step in workflow["jobs"]["promote"]["steps"])
    for required in (
        "certification/promotions/[0-9]+", "check_promotion_readiness.py", "burnStartCommit",
        "git log", "platform-lock.json", "strictTrains", "demoCanaries",
        "steps.readiness.outputs.rc_train_run_id", "candidate/platform-lock.json",
    ):
        assert required in commands
    assert "docker build" not in commands
    assert "dotnet build" not in commands
    assert "npm pack" not in commands


def test_promotion_request_uses_the_scoped_claude_app_identity():
    workflow = _workflow("request-promotion.yml")
    triggers = _triggers(workflow)
    assert set(triggers) == {"workflow_run"}, "the human reviewer must not have a redispatch trigger"
    assert triggers["workflow_run"] == {"workflows": ["release-train"], "types": ["completed"]}
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}

    dispatch = workflow["jobs"]["dispatch"]
    assert "conclusion == 'success'" in dispatch["if"]
    assert "event == 'workflow_dispatch'" in dispatch["if"]
    assert "head_branch == github.event.repository.default_branch" in dispatch["if"]

    steps = dispatch["steps"]
    app_token = next(step for step in steps if step.get("id") == "app-token")
    assert app_token["uses"].startswith("actions/create-github-app-token@")
    assert app_token["with"] == {
        "app-id": "${{ vars.CLAUDE_APP_ID }}",
        "private-key": "${{ secrets.CLAUDE_APP_PRIVATE_KEY }}",
        "owner": "${{ github.repository_owner }}",
        "repositories": "honua-release",
    }

    commands = "\n".join(_step_text(step) for step in steps)
    assert "RELEASE_GH_TOKEN" not in commands
    assert "gh workflow run promote.yml" in commands
    promote_dispatch = next(step for step in steps if "gh workflow run promote.yml" in _step_text(step))
    assert promote_dispatch["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"


def test_repo_control_drift_check_is_read_only():
    workflow = _workflow("repo-control-drift.yml")
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}

    jobs = workflow["jobs"].values()
    commands = "\n".join(_step_text(step) for job in jobs for step in job["steps"])
    assert "tools/check_release_promotion_approval.py" in commands
    assert "--mode drift" in commands

    assert_read_only(commands, "the drift monitor")


def test_contract_gate_certifies_the_breaking_change_suppression_state():
    workflow = _workflow("gate-contract.yml")
    jobs = workflow["jobs"]
    assert "breaking-change-suppression" in jobs
    # The suppression verdict must reach the gate report, not just the job log.
    assert "breaking-change-suppression" in jobs["report"]["needs"]

    suppression = jobs["breaking-change-suppression"]
    assert not _neutralised(suppression)
    commands = "\n".join(_step_text(step) for step in suppression["steps"])
    assert "tools/check_contract_suppression.py" in commands
    assert "certification/contract-suppression-policy.yaml" in commands
    # The fragment is emitted even when the job fails, and defaults to a non-green status.
    fragment = next(
        step for step in suppression["steps"] if "gate-fragment" in str(step.get("uses", ""))
    )
    assert fragment.get("if") == "always()"
    assert "blocked" in str(fragment["with"]["status"])


def test_contract_gate_report_cannot_be_assembled_from_survivors():
    """A cancelled check must not silently disappear from the gate report."""
    report = _workflow("gate-contract.yml")["jobs"]["report"]
    commands = "\n".join(_step_text(step) for step in report["steps"])
    for gate in ("contract-proto", "contract-rest-sdk", "contract-suppression"):
        assert gate in commands, f"{gate} is not required by name in the report job"
    assert '"fail"' in commands, "a missing fragment must be recorded as a failure"


def test_upgrade_gate_proves_a_seeded_forward_migration_and_prior_image_compatibility():
    """The kind lane must not regress to same-schema, empty-database lifecycle theatre."""
    workflow = _workflow("gate-upgrade.yml")
    commands = "\n".join(
        _step_text(step) for step in workflow["jobs"]["kind-upgrade"]["steps"]
    )

    assert "platform-manifest.yaml" in commands
    assert "FIRST_RELEASE_BASELINE_IMAGE" in commands
    assert "gh release download \"$PREV\"" in commands
    assert "e2e/harness/seed/seed.sh" in commands
    assert "SELECT count(*) FROM public.schema_versions" in commands
    assert "AFTER_VERSIONS" in commands and '"-gt"' not in commands
    assert "CANDIDATE_SCHEMA_FLOOR" in commands
    assert "SELECT count(*) FROM honua_data.e2e_src_fs" in commands
    assert "SELECT count(*) FROM honua_data.maui_zoning" in commands
    assert "string_agg(to_jsonb(v)::text" in commands
    assert "ORDER BY to_jsonb(v)::text" in commands
    assert "/tmp/upg/rollback-fault-injection.log" in commands
    assert ">> /tmp/upg/candidate.log" not in commands
    assert "config.env.Operations__Policy__Enabled=true" in commands
    assert "config.env.Operations__Policy__DefaultDecision=Deny" in commands
    assert "helm rollback honua 1" in commands
    assert "returnCountOnly=true" in commands
    assert "down-migration noted" not in commands


def test_release_train_requires_signed_one_operation_rollback_certification():
    workflow = _workflow("release-train.yml")
    assert "gate_one_operation_rollback" in workflow["jobs"]
    rollback = workflow["jobs"]["gate_one_operation_rollback"]
    assert rollback["uses"] == "./.github/workflows/rollback-certification.yml"
    report = workflow["jobs"]["report"]
    assert "gate_one_operation_rollback" in report["needs"]
    commands = "\n".join(_step_text(step) for step in report["steps"])
    assert "one-operation-rollback|$S_ROLLBACK" in commands


def test_rollback_certification_signs_success_and_mixed_state_receipts():
    workflow = _workflow("rollback-certification.yml")
    commands = "\n".join(_step_text(step) for step in workflow["jobs"]["certify"]["steps"])
    assert "test_release_rollback.py" in commands
    assert "certify_release_rollback.py" in commands
    assert "Succeeded" in commands and "ManualInterventionRequired" in commands
    assert "candidate-manifest" in commands
    assert "gh attestation verify _candidate/platform-lock.json" in commands
    assert "gh attestation verify _retained/platform-lock.json" in commands
    assert "--candidate-manifest _candidate/platform-manifest.yaml" in commands
    assert "--compatibility-matrix _candidate/compatibility-matrix.yaml" in commands
    rendered = str(workflow["jobs"]["certify"])
    assert rendered.count("actions/attest-build-provenance@") == 2


def test_upgrade_failure_game_day_aggregates_every_matrix_cell_and_uses_unlicensed_write_probe():
    workflow = _workflow("gate-upgrade.yml")
    kind_commands = "\n".join(_step_text(step) for step in workflow["jobs"]["kind-upgrade"]["steps"])
    assert "upg*-gate-fragment-*" in str(workflow["jobs"]["report"])
    assert "/api/v1/admin/services/e2e/access-policy" in kind_commands
    assert "FeatureServer/$SRC_ID/applyEdits" not in kind_commands
    assert "rollback-failure" in kind_commands and "migration-boundary" in kind_commands


def test_release_train_requires_signed_one_operation_rollback_certification():
    workflow = _workflow("release-train.yml")
    assert "gate_one_operation_rollback" in workflow["jobs"]
    rollback = workflow["jobs"]["gate_one_operation_rollback"]
    assert rollback["uses"] == "./.github/workflows/rollback-certification.yml"
    report = workflow["jobs"]["report"]
    assert "gate_one_operation_rollback" in report["needs"]
    commands = "\n".join(_step_text(step) for step in report["steps"])
    assert "one-operation-rollback|$S_ROLLBACK" in commands


def test_rollback_certification_signs_success_and_mixed_state_receipts():
    workflow = _workflow("rollback-certification.yml")
    commands = "\n".join(_step_text(step) for step in workflow["jobs"]["certify"]["steps"])
    assert "test_release_rollback.py" in commands
    assert "certify_release_rollback.py" in commands
    assert "Succeeded" in commands and "ManualInterventionRequired" in commands
    rendered = str(workflow["jobs"]["certify"])
    assert rendered.count("actions/attest-build-provenance@") == 2


# ── the read-only scanner must itself be proven, not assumed ────────────────────────────────────
#
# A clean shipped workflow is not evidence that the check works — it is equally consistent with a
# check that never fires. These cases exercise the scanner directly, including the two evasions that
# survived the first version of it.

READ_ONLY_COMMANDS = (
    'gh api "repos/$REPOSITORY/environments/$ENVIRONMENT" > environment.json 2>/tmp/env.err',
    'gh api "repos/$REPOSITORY" > repository.json 2>/dev/null || true',
    'gh api "repos/$REPOSITORY/actions/variables" --paginate > variables.json',
    'gh api "repos/$O/$R" --jq .default_branch',
    'if [ -f repository.json ]; then gh api "repos/$O/$R" > branch.json; fi',
    'gh api \\\n  "repos/$O/$R/environments/release-promotion" \\\n  --jq .name',
)

MUTATING_COMMANDS = (
    # the two evasions the re-review found
    'gh api \\\n  --method PUT \\\n  "repos/$O/$R/environments/release-promotion"',
    'gh api "repos/$O/$R/environments/release-promotion" -fwait_timer=0',
    # and the forms the literal list already covered, re-proven through the new scanner
    'gh api -X PATCH "repos/$O/$R/environments/release-promotion"',
    'gh api --method DELETE "repos/$O/$R/environments/release-promotion"',
    'gh api -f prevent_self_review=false "repos/$O/$R/environments/release-promotion"',
    'gh api -F reviewers[][id]=1 "repos/$O/$R/environments/release-promotion"',
    'gh api --field key=value "repos/$O/$R"',
    'gh api --raw-field key=value "repos/$O/$R"',
    'gh api --input payload.json "repos/$O/$R/environments/release-promotion"',
    'gh api \\\n  "repos/$O/$R/environments/release-promotion" \\\n  -Fprevent_self_review=false',
    'gh secret set RELEASE_GH_TOKEN --body "$X"',
    'gh variable set OPENAPI_ALLOW_BREAKING_CHANGES --body true',
    'gh release create honua-2026.1',
    'gh api graphql -f query="mutation {}"',
)


@pytest.mark.parametrize("command", READ_ONLY_COMMANDS)
def test_read_only_scanner_accepts_genuine_reads(command: str):
    assert_read_only(command, "a read-only fixture")


@pytest.mark.parametrize("command", MUTATING_COMMANDS)
def test_read_only_scanner_catches_every_mutating_form(command: str):
    with pytest.raises(AssertionError):
        assert_read_only(command, "a mutating fixture")


def test_logical_lines_join_continuations():
    joined = _logical_lines('gh api \\\n  --method PUT \\\n  "repos/o/r"')
    assert joined == ['gh api --method PUT "repos/o/r"']


# ── the committed registers must load through the checkers that consume them ────────────────────
#
# A policy file that the checker would refuse is otherwise only discovered at promote time — during a
# release, which is the worst possible moment to find out.

SHIPPED_REGISTERS = {
    "tools/check_release_promotion_approval.py": (
        "certification/release-promotion-approval.yaml",
        cpa.load_policy,
    ),
    "tools/check_contract_suppression.py": (
        "certification/contract-suppression-policy.yaml",
        ccs.load_policy,
    ),
}


@pytest.mark.parametrize("script", sorted(SHIPPED_REGISTERS))
def test_the_shipped_register_loads_through_its_checker(script: str):
    register, loader = SHIPPED_REGISTERS[script]
    assert (REPO_ROOT / script).is_file()
    loader(REPO_ROOT / register)  # raises if the committed file would be refused at release time


@pytest.mark.parametrize(
    "workflow_name", ["promote.yml", "repo-control-drift.yml", "gate-contract.yml"]
)
def test_workflows_point_their_checkers_at_the_shipped_register(workflow_name: str):
    workflow = _workflow(workflow_name)
    commands = "\n".join(
        _step_text(step) for job in workflow["jobs"].values() for step in job.get("steps", [])
    )
    for script, (register, _) in SHIPPED_REGISTERS.items():
        if script not in commands:
            continue
        referenced = set(re.findall(r"--policy\s+(\S+)", commands))
        assert referenced == {register}, (
            f"{workflow_name} runs {script} against {referenced}, not the shipped {register}"
        )


def test_cloud_teardown_reaper_is_fail_closed():
    # honua-iac#142: a cell that provisioned real AWS infrastructure and could not clean it up must
    # redden the run. A swallowed reaper turns a stranded VPC/cluster into a silent monthly bill.
    workflow = _workflow("e2e-cloud-aws.yml")
    steps = workflow["jobs"]["parity"]["steps"]
    parity = next(step for step in steps if str(step.get("name", "")).startswith("Run ${{ matrix.target }}"))
    reaper = next(step for step in steps if step.get("name") == "Teardown reaper (backstop)")

    marker = parity["env"]["HONUA_CLOUD_PROVISION_MARKER"]
    assert marker and reaper["env"]["HONUA_CLOUD_PROVISION_MARKER"] == marker
    assert reaper["if"] == "always()"
    assert '-f "$HONUA_CLOUD_PROVISION_MARKER"' in reaper["run"]
    assert reaper["run"].index('-f "$HONUA_CLOUD_PROVISION_MARKER"') < reaper["run"].index(
        "python e2e/reap_cloud.py"
    )
    assert "|| true" not in reaper["run"]


def test_cloud_parity_resolves_the_runner_cidr_for_every_target():
    # The EKS cell publishes its API server and its load balancer to the runner's /32 and nothing
    # else, so the address has to be resolved for every cell, not just the RDS-backed ones.
    workflow = _workflow("e2e-cloud-aws.yml")
    steps = workflow["jobs"]["parity"]["steps"]
    ingress = next(step for step in steps if str(step.get("name", "")).startswith("Resolve the ephemeral runner"))

    assert "matrix.target" not in str(ingress.get("if", ""))
    assert "HONUA_AWS_RUNNER_CIDR=${RUNNER_IP}/32" in ingress["run"]
    assert "HONUA_AWS_DB_INGRESS_CIDR=${RUNNER_IP}/32" in ingress["run"]


def test_cloud_parity_installs_the_declared_runner_dependencies_before_self_test():
    workflow = _workflow("e2e-cloud-aws.yml")
    steps = workflow["jobs"]["parity"]["steps"]
    install_index = next(index for index, step in enumerate(steps)
                         if step.get("name") == "Install cloud runner dependencies")
    self_test_index = next(index for index, step in enumerate(steps)
                           if step.get("name") == "Self-test the cloud parity logic")

    assert "-r e2e/requirements.txt" in steps[install_index]["run"]
    assert install_index < self_test_index
