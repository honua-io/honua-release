"""The customer gate cannot admit another image, skipped cases or a partial journey."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
import yaml
import studio_customer_gate as g


def _workflow():
    return yaml.safe_load(Path('.github/workflows/gate-studio-customer-journey.yml').read_text())


def _rederive_step(steps):
    return next(index for index, step in enumerate(steps) if 'platform_lock_bundle.py' in step.get('run', ''))


def test_workflow_installs_lock_validator_dependencies_before_validation():
    steps = _workflow()['jobs']['customer']['steps']
    validate = _rederive_step(steps)
    installs = '\n'.join(step.get('run', '') for step in steps[:validate])
    assert all(dep in installs.lower() for dep in ('pyyaml', 'jsonschema', 'cryptography'))


def test_qualification_lock_is_rederived_from_attested_inputs_with_no_fallback():
    """A freeze_only lock is unsigned: the gate verifies the attested manifest and matrix, re-derives
    the lock from those bytes, and requires byte equality plus the dispatched digest."""
    steps = _workflow()['jobs']['customer']['steps']
    run = steps[_rederive_step(steps)]['run']
    assert run.lstrip().startswith('set -euo pipefail')
    for name in ('platform-manifest.yaml', 'compatibility-matrix.yaml'):
        assert name in run
    assert 'gh attestation verify "candidate-lock/$input" --repo' in run
    assert '--signer-workflow "${{ github.repository }}/.github/workflows/release-train.yml"' in run
    assert ('--manifest candidate-lock/platform-manifest.yaml --matrix candidate-lock/compatibility-matrix.yaml'
            in run and '--qualification' in run)
    assert 'cmp candidate-lock/platform-lock.json "$RUNNER_TEMP/rederived-lock/platform-lock.json"' in run
    assert '[ "$actual" = "$LOCK_DIGEST" ]' in run
    # The lock's own unsigned bytes are never trusted through another path.
    every_run = '\n'.join(step.get('run', '') for step in steps)
    assert 'gh attestation verify candidate-lock/platform-lock.json' not in every_run
    assert '|| true' not in run
    gate = next(step['run'] for step in steps if 'studio_customer_gate.py' in step.get('run', ''))
    assert '--manifest candidate-lock/platform-manifest.yaml' in gate
    assert steps.index(next(s for s in steps if 'studio_customer_gate.py' in s.get('run', ''))) > _rederive_step(steps)


def test_lock_artifact_defaults_name_the_release_train_artifact():
    on = _workflow()[True]
    assert on['workflow_dispatch']['inputs']['lock_artifact']['default'] == 'candidate-manifest'
    assert on['workflow_call']['inputs']['lock_artifact']['default'] == 'candidate-manifest'
    arc = yaml.safe_load(Path('.github/workflows/ai-arc.yml').read_text())[True]
    assert arc['workflow_dispatch']['inputs']['lock_artifact']['default'] == 'candidate-manifest'
    train = Path('.github/workflows/release-train.yml').read_text()
    assert 'name: candidate-manifest' in train and '            platform-lock.json\n' in train


def test_lock_must_bind_exact_bytes_manifest_and_both_component_pins():
    manifest = b"candidate manifest bytes"
    selected = g.resolve_pins({"components": pins()})
    lock = {"sourceInputs": {"platformManifest": {"sha256": "sha256:" + hashlib.sha256(manifest).hexdigest()}},
            "components": {f"honua-{name}": {"artifacts": [{"kind": "image", "coordinate": f"ghcr.io/honua-io/honua-{name}",
                "digest": value["digest"], "sourceRevision": value["sha"]}]} for name, value in selected.items()}}
    raw = json.dumps(lock).encode()
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    g.validate_lock(raw, digest, manifest, selected)
    with pytest.raises(ValueError):
        g.validate_lock(raw, "sha256:" + "f" * 64, manifest, selected)
    with pytest.raises(ValueError):
        g.validate_lock(raw, digest, b"other manifest", selected)
    for name in ("console", "server"):
        for key in ("digest", "sourceRevision", "coordinate"):
            altered = deepcopy(lock)
            altered["components"][f"honua-{name}"]["artifacts"][0][key] = "wrong"
            altered_raw = json.dumps(altered).encode()
            with pytest.raises(ValueError):
                g.validate_lock(altered_raw, "sha256:" + hashlib.sha256(altered_raw).hexdigest(), manifest, selected)


def pins():
    return {name: {"image": f"ghcr.io/honua-io/{name}:candidate", "digest": "sha256:" + char * 64,
                   "sha": char * 40} for name, char in [("honua-console", "a"), ("honua-server", "b")]}


def evidence():
    selected = g.resolve_pins({"components": pins()})
    runtime = {name: {"requested": value["image"], "sourceRevision": value["sha"]}
               for name, value in selected.items()}
    runtime["suiteSourceRevision"] = selected["console"]["sha"]
    report = {"stats": {"expected": 2, "unexpected": 0, "flaky": 0, "skipped": 0}}
    return selected, runtime, report, {"qualificationComplete": True, "completed": list(g.REQUIRED_STAGES)}


def test_exact_images_and_complete_customer_evidence_pass():
    g.validate_evidence(*evidence())


@pytest.mark.parametrize("component", ["console", "server"])
def test_another_digest_or_source_is_rejected(component):
    for key in ["requested", "sourceRevision"]:
        values = deepcopy(evidence())
        values[1][component][key] = "wrong"
        with pytest.raises(ValueError):
            g.validate_evidence(*values)


@pytest.mark.parametrize("field", ["unexpected", "flaky", "skipped"])
def test_nonpassing_tests_are_rejected(field):
    values = evidence()
    values[2]["stats"][field] = 1
    with pytest.raises(ValueError):
        g.validate_evidence(*values)


def test_partial_or_no_customer_journey_is_rejected():
    for progress in [{}, {"qualificationComplete": False}, {"qualificationComplete": True, "completed": []}]:
        values = evidence()
        with pytest.raises(ValueError):
            g.validate_evidence(*values[:3], progress)


def test_previous_non_admin_receipt_cannot_replace_the_publisher_journey():
    values = evidence()
    completed = set(g.REQUIRED_STAGES) - {"publisher-auth"}
    completed.add("non-admin-auth")
    with pytest.raises(ValueError, match="partial"):
        g.validate_evidence(*values[:3], {"qualificationComplete": True, "completed": list(completed)})


def test_publication_without_separate_approval_is_rejected():
    values = evidence()
    completed = g.REQUIRED_STAGES - {"separate-approval"}
    with pytest.raises(ValueError, match="partial"):
        g.validate_evidence(*values[:3], {"qualificationComplete": True, "completed": list(completed)})


def test_tag_only_or_mismatched_digest_is_rejected():
    for digest in ["nightly", "", "sha256:short"]:
        components = pins()
        components["honua-console"]["digest"] = digest
        with pytest.raises(ValueError):
            g.resolve_pins({"components": components})
    components = pins()
    components["honua-console"]["image"] += "@sha256:" + "c" * 64
    with pytest.raises(ValueError):
        g.resolve_pins({"components": components})


def test_zero_tests_and_wrong_suite_revision_are_rejected():
    values = evidence()
    values[2]["stats"]["expected"] = 0
    with pytest.raises(ValueError):
        g.validate_evidence(*values)
    values = evidence()
    values[1]["suiteSourceRevision"] = "c" * 40
    with pytest.raises(ValueError):
        g.validate_evidence(*values)


def test_dispatch_uses_verified_tag_at_exact_console_commit(monkeypatch):
    refs = {}
    calls = []
    sha = 'a' * 40
    def fake_gh(*args, json_output=False):
        calls.append(args)
        if args[:2] == ('api', '--method'):
            assert args[2:4] == ('POST', f'repos/{g.REPO}/git/refs')
            ref = next(x[4:] for x in args if x.startswith('ref='))
            target = next(x[4:] for x in args if x.startswith('sha='))
            refs[ref] = target
            return ''
        if args[0] == 'api':
            ref = 'refs/' + args[1].split('/git/ref/', 1)[1]
            return {'object': {'type': 'commit', 'sha': refs[ref]}}
        if args[:2] == ('workflow', 'run'):
            ref = args[args.index('--ref') + 1]
            assert ref in refs and refs[ref] == sha  # GitHub rejects a raw commit SHA.
            return ''
        raise AssertionError(args)
    monkeypatch.setattr(g, 'gh', fake_gh)
    selected = g.resolve_pins({'components': pins()})
    g.dispatch_customer(selected, 'release-test')
    assert any(call[:2] == ('workflow', 'run') for call in calls)


def test_dispatch_rejects_tag_pointing_to_another_commit(monkeypatch):
    def fake_gh(*args, json_output=False):
        if args[:2] == ('api', '--method'):
            return ''
        if args[0] == 'api':
            return {'object': {'type': 'commit', 'sha': 'f' * 40}}
        raise AssertionError('must not dispatch a mismatched tag')
    monkeypatch.setattr(g, 'gh', fake_gh)
    with pytest.raises(ValueError, match='tag'):
        g.dispatch_customer(g.resolve_pins({'components': pins()}), 'release-test')


# Release-session ruling 2026-11-03: harness-only suite exception (e2e/** descendants of the pinned Console sha).
PINNED, SUITE = "a" * 40, "b" * 40


def compare(files, *, status="ahead", behind=0, base=PINNED, merge_base=PINNED, last=SUITE, ahead=1):
    return {"status": status, "ahead_by": ahead, "behind_by": behind, "total_commits": ahead,
            "base_commit": {"sha": base}, "merge_base_commit": {"sha": merge_base},
            "commits": [{"sha": last}], "files": [{"filename": name} for name in files]}


def test_suite_exception_admits_an_e2e_only_descendant_and_records_its_paths():
    changed = g.suite_exception(PINNED, SUITE, compare(["e2e/agent-reflection/run-quickstart.py",
                                                        "e2e/agent-reflection/test_secrets.py"]))
    assert changed == ["e2e/agent-reflection/run-quickstart.py", "e2e/agent-reflection/test_secrets.py"]


@pytest.mark.parametrize("files", [
    ["e2e/agent-reflection/run-quickstart.py", "src/Honua.Console.Web/Program.cs"],
    ["Dockerfile"], [".github/workflows/console-live-journey.yml"], ["e2e-tools/x.py"], ["e2e/../src/x"], []])
def test_suite_exception_refuses_any_path_outside_e2e(files):
    with pytest.raises(ValueError):
        g.suite_exception(PINNED, SUITE, compare(files))


def test_suite_exception_refuses_a_rename_out_of_or_into_e2e():
    value = compare(["e2e/moved.py"])
    value["files"][0]["previous_filename"] = "src/moved.py"
    with pytest.raises(ValueError, match="outside e2e"):
        g.suite_exception(PINNED, SUITE, value)


@pytest.mark.parametrize("kwargs", [dict(status="diverged"), dict(behind=1), dict(base="c" * 40),
                                    dict(merge_base="c" * 40), dict(last="c" * 40), dict(ahead=0)])
def test_suite_exception_refuses_a_non_descendant_or_mismatched_compare(kwargs):
    with pytest.raises(ValueError):
        g.suite_exception(PINNED, SUITE, compare(["e2e/x.py"], **kwargs))


def test_suite_exception_refuses_truncated_lists_and_non_sha_inputs():
    with pytest.raises(ValueError, match="truncated"):
        g.suite_exception(PINNED, SUITE, compare([f"e2e/f{i}.py" for i in range(300)]))
    for pinned, suite in ((PINNED, PINNED), ("a" * 39, SUITE), (PINNED, "main")):
        with pytest.raises(ValueError):
            g.suite_exception(pinned, suite, compare(["e2e/x.py"]))


def test_evidence_binds_the_approved_suite_while_the_image_stays_the_pinned_one():
    values = evidence()
    pinned = values[0]["console"]["sha"]
    values[1]["suiteSourceRevision"] = "c" * 40
    g.validate_evidence(*values, suite_sha="c" * 40)
    with pytest.raises(ValueError):
        g.validate_evidence(*values)                       # without the exception the equality rule holds
    values[1]["console"]["sourceRevision"] = "c" * 40      # the image itself must stay the pinned source
    with pytest.raises(ValueError, match="console runtime"):
        g.validate_evidence(*values, suite_sha="c" * 40)
    assert pinned != "c" * 40


def test_dispatch_tags_the_suite_commit_when_the_exception_applies(monkeypatch):
    refs = {}
    def fake_gh(*args, json_output=False):
        if args[:2] == ('api', '--method'):
            refs[next(x[4:] for x in args if x.startswith('ref='))] = next(x[4:] for x in args if x.startswith('sha='))
            return ''
        if args[0] == 'api':
            return {'object': {'type': 'commit', 'sha': refs['refs/' + args[1].split('/git/ref/', 1)[1]]}}
        if args[:2] == ('workflow', 'run'):
            assert refs[args[args.index('--ref') + 1]] == SUITE
            assert any(x == f'console_image={g.resolve_pins({"components": pins()})["console"]["image"]}' for x in args)
            return ''
        raise AssertionError(args)
    monkeypatch.setattr(g, 'gh', fake_gh)
    g.dispatch_customer(g.resolve_pins({'components': pins()}), 'release-test', SUITE)


def test_workflow_passes_the_optional_suite_sha_and_studio_job_is_advisory_for_build_test():
    workflow = _workflow()
    for trigger in ('workflow_dispatch', 'workflow_call'):
        assert workflow[True][trigger]['inputs']['console_suite_sha']['default'] == ''
    step = next(s for s in workflow['jobs']['customer']['steps'] if 'studio_customer_gate.py' in s.get('run', ''))
    assert '--suite-sha "$CONSOLE_SUITE_SHA"' in step['run']
    assert step['env']['CONSOLE_SUITE_SHA'] == '${{ inputs.console_suite_sha }}'
    advisory = yaml.safe_load(Path('certification/advisory-checks.yaml').read_text())
    assert "Studio customer journey on real images" in [item['name'] for item in advisory['honua-console']]
