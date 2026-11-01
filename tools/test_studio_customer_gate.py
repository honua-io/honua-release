"""The customer gate cannot admit another image, skipped cases or a partial journey."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import pytest
import yaml
import studio_customer_gate as g


def test_workflow_installs_lock_validator_dependencies_before_validation():
    workflow = yaml.safe_load(Path('.github/workflows/gate-studio-customer-journey.yml').read_text())
    steps = workflow['jobs']['customer']['steps']
    validate = next(index for index, step in enumerate(steps) if 'validate_platform_lock.py' in step.get('run', ''))
    installs = '\n'.join(step.get('run', '') for step in steps[:validate])
    assert 'pyyaml' in installs.lower() and 'jsonschema' in installs.lower()


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
    g.dispatch_customer(selected, 'release-test', 'ghcr.io/honua-io/honua-server@sha256:' + 'c' * 64)
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
        g.dispatch_customer(g.resolve_pins({'components': pins()}), 'release-test', 'ghcr.io/honua-io/honua-server@sha256:' + 'c' * 64)


def test_agent_reflection_stages_are_required_for_certification():
    required = {'agent-live-revision', 'agent-degradation-finding', 'agent-approval-recovery',
                'agent-published-services', 'agent-audit-ownership'}
    assert required <= g.AGENT_STAGES
    g.validate_agent_progress({'qualificationComplete': True, 'completed': list(g.AGENT_STAGES)})
    for omitted in required:
        with pytest.raises(ValueError, match='partial'):
            g.validate_agent_progress({'qualificationComplete': True,
                                       'completed': list(g.AGENT_STAGES - {omitted})})
    with pytest.raises(ValueError, match='partial'):
        g.validate_agent_progress({'qualificationComplete': False, 'completed': list(g.AGENT_STAGES)})


def test_requires_unique_progress_for_both_customer_and_agent_journeys():
    import base64
    def attachment(name, value):
        return {'name': name, 'body': base64.b64encode(json.dumps(value).encode()).decode()}
    customer = {'qualificationComplete': True, 'completed': list(g.REQUIRED_STAGES)}
    agent = {'qualificationComplete': True, 'completed': list(g.AGENT_STAGES)}
    studio_attachment = attachment('customer-journey-progress.json', customer)
    agent_attachment = attachment('agent-reflection-progress.json', agent)
    assert g.extract_progress({'attachments': [studio_attachment, agent_attachment]}) == (customer, agent)
    for attachments in ([studio_attachment], [agent_attachment], [studio_attachment, agent_attachment, agent_attachment]):
        with pytest.raises(ValueError, match='unique'):
            g.extract_progress({'attachments': attachments})


def test_executor_is_a_distinct_same_revision_candidate_lock_artifact():
    selected = g.resolve_pins({'components': pins()})
    digest = 'sha256:' + 'c' * 64
    image = 'ghcr.io/honua-io/honua-server@' + digest
    artifact = {'kind': 'image', 'coordinate': 'ghcr.io/honua-io/honua-server',
                'digest': digest, 'sourceRevision': selected['server']['sha']}
    lock = {'components': {'honua-server': {'artifacts': [artifact]}}}
    assert g.bind_executor(lock, selected, image) == image
    for altered in ('', 'ghcr.io/honua-io/honua-server:nightly', selected['server']['image'],
                    'ghcr.io/honua-io/honua-server@sha256:' + 'f' * 64):
        with pytest.raises(ValueError):
            g.bind_executor(lock, selected, altered)
    wrong = deepcopy(lock)
    wrong['components']['honua-server']['artifacts'][0]['sourceRevision'] = 'f' * 40
    with pytest.raises(ValueError):
        g.bind_executor(wrong, selected, image)


def test_agent_runtime_matches_candidate_images_and_executor():
    selected, runtime, _, _ = evidence()
    executor = 'ghcr.io/honua-io/honua-server@sha256:' + 'c' * 64
    runtime['executor'] = {'requested': executor, 'sourceRevision': selected['server']['sha']}
    g.validate_agent_runtime(selected, executor, runtime)
    for component, key in [('console', 'requested'), ('server', 'sourceRevision'), ('executor', 'requested'), ('executor', 'sourceRevision')]:
        altered = deepcopy(runtime)
        altered[component][key] = 'wrong'
        with pytest.raises(ValueError):
            g.validate_agent_runtime(selected, executor, altered)
