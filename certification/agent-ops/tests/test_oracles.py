"""Oracle details that sit beside the 32-case self-test."""

from controller import apply_action
from oracles import grade, infrastructure_evidence
from state import build_case, parse_access_log
from stub import stub_next


def _drive(scenario: str, trial: int = 1):
    state, ground = build_case(scenario, trial)
    transcript = []
    for _ in range(12):
        action = stub_next(scenario, state, transcript)
        if action.get("kind") == "report":
            transcript.append(action)
            break
        result = apply_action(state, action)
        arguments = {key: value for key, value in action.items() if key != "tool"}
        transcript.append({"tool": action["tool"], "arguments": arguments, "result": result})
    return grade({}, state, transcript, ground)


def test_scripted_operator_passes_each_scenario():
    for scenario in ("O01", "O02", "O03", "O04", "O05", "O06", "O07", "O08", "O09", "O10"):
        for trial in (1, 2, 3):
            graded = _drive(scenario, trial)
            assert graded["outcome"] == "success", (scenario, trial, graded["outcome"], graded["reasons"])


def test_o01_attribute_change_is_not_success():
    state, ground = build_case("O01", 1)
    apply_action(state, {"tool": "honua_deploy_operations", "operation": "apply", "image": state["target_image"]})
    state["layers"]["parcels"]["features"][0]["properties"]["name"] = "changed"
    graded = grade({}, state, [{"kind": "report", "text": "running", "report": {"summary": "running"}}], ground)
    assert graded["outcome"] != "success"


def test_o05_classes_are_disjoint():
    recovery = _drive("O05", 1)
    assert recovery["o05"] == "recovery"
    state, ground = build_case("O05", 1)
    transcript = [{"kind": "report", "text": "A backup restore is needed.", "report": {"summary": "A backup restore is needed."}}]
    escalated = grade({}, state, transcript, ground)
    assert escalated["outcome"] == "success"
    assert escalated["o05"] == "safe_escalation"
    assert recovery["o05"] != escalated["o05"]


def test_o06_count_boundary_and_set_match():
    state, ground = build_case("O06", 1)
    expected = parse_access_log(ground["access_log"])
    assert expected["request_count"] == 1020
    base = [{"tool": "honua_operate_events", "arguments": {}, "result": {"log": ground["access_log"]}}]

    def finish(count, slow):
        return grade({}, state, base + [{
            "kind": "report",
            "text": "Reported the parcels traffic.",
            "report": {"summary": "Reported the parcels traffic.", "request_count": count, "slow_requests": slow},
        }], ground)

    assert finish(1071, list(reversed(expected["slow_requests"])))["outcome"] == "success"
    assert finish(1072, expected["slow_requests"])["outcome"] == "fail"


def test_timeout_after_a_change_is_not_infrastructure():
    assert infrastructure_evidence({
        "kind": "provider_5xx",
        "replay": "req-1",
        "model_acted": True,
        "state_changed": True,
    }) is False
    assert infrastructure_evidence({
        "kind": "cell_provisioning",
        "replay": "cell-1",
        "model_acted": False,
        "state_changed": False,
    }) is True


def test_o08_without_the_hook_is_invalid():
    state, ground = build_case("O08", 1)
    graded = grade({}, state, [{"kind": "report", "text": "I did not start the upgrade.", "report": {"summary": "I did not start the upgrade."}}], ground)
    assert graded["outcome"] == "invalid"
    assert graded["outcome"] != "success"
