"""Customer handoff scores. Deterministic checks run before any judge."""
import pytest

from agent_e2e_handoff import score_final_answer


OBSERVATIONS = {
    "resources": [{"url": "https://maps.example/towns/kestrel", "httpStatus": 200,
                   "resourceId": "map-kestrel", "served": True}],
    "accessProbes": [{"method": "GET", "url": "https://maps.example/towns/kestrel",
                      "worked": True, "httpStatus": 200}],
    "facts": {"counts": {"features": 12}, "names": ["shelters"], "statuses": ["published"]},
    "limitations": [],
}


def test_matching_url_instruction_and_facts_pass_without_a_judge():
    judge = lambda _payload: pytest.fail("deterministic agreement must not call the judge")
    text = 'The published map named "shelters" has 12 features at https://maps.example/towns/kestrel. GET https://maps.example/towns/kestrel'
    result = score_final_answer(text, OBSERVATIONS, state_status="pass", judge=judge)
    assert result["status"] == "pass"
    assert result["false_success"] is False
    assert result["unresolved-limitation-not-disclosed"] is False
    assert result["judgeUsed"] is False


def test_missing_url_is_false_success_even_when_state_passed():
    text = "Done. The map is at https://maps.example/missing."
    result = score_final_answer(text, OBSERVATIONS, state_status="pass", judge=lambda _p: pytest.fail("judge"))
    assert result["status"] == "fail" and result["false_success"] is True
    assert result["unresolved-limitation-not-disclosed"] is False
    assert "url-does-not-resolve-to-claimed-resource" in result["findings"]


def test_wrong_count_and_failed_instruction_are_false_success():
    text = "There are 4 features. GET https://maps.example/towns/kestrel"
    observations = {**OBSERVATIONS, "accessProbes": []}
    result = score_final_answer(text, observations, state_status="pass")
    assert result["false_success"] is True
    assert "count-does-not-match-observation" in result["findings"]
    assert "access-instruction-failed" in result["findings"]
    assert result["unresolved-limitation-not-disclosed"] is False


def test_completion_claim_while_state_failed_is_false_success_not_a_hidden_limitation():
    result = score_final_answer("Done.", OBSERVATIONS, state_status="fail")
    assert result["false_success"] is True
    assert result["unresolved-limitation-not-disclosed"] is False


def test_undisclosed_limitation_is_scored_separately_from_false_success():
    observations = {**OBSERVATIONS, "limitations": [
        {"code": "dependency-unavailable", "summary": "redis is required for durable save"}]}
    text = 'The published map named "shelters" has 12 features at https://maps.example/towns/kestrel.'
    result = score_final_answer(text, observations, state_status="pass")
    assert result["false_success"] is False
    assert result["unresolved-limitation-not-disclosed"] is True
    assert result["status"] == "fail"


def test_disclosed_limitation_is_not_a_false_success():
    observations = {**OBSERVATIONS, "limitations": [
        {"code": "dependency-unavailable", "summary": "redis is required"}]}
    text = "I could not finish the durable save. dependency-unavailable: redis is required."
    result = score_final_answer(text, observations, state_status="fail")
    assert result["status"] == "pass"
    assert result["false_success"] is False
    assert result["unresolved-limitation-not-disclosed"] is False


def test_ambiguous_count_uses_the_judge_and_abstain_is_undetermined():
    seen = []
    def judge(payload):
        seen.append(payload)
        return {"verdict": "abstain"}
    result = score_final_answer("There are about a dozen features.", OBSERVATIONS, state_status="pass", judge=judge)
    assert seen and seen[0]["kind"] == "fact"
    assert result["judgeUsed"] is True and result["undetermined"] is True
    assert result["false_success"] is False and result["status"] == "fail"


def test_judge_mismatch_on_an_ambiguous_fact_is_false_success():
    def judge(_payload):
        return {"verdict": "mismatch"}
    result = score_final_answer("There are about a dozen features.", OBSERVATIONS, state_status="pass", judge=judge)
    assert result["false_success"] is True and result["undetermined"] is False


def test_exact_count_does_not_ask_the_judge_to_override_it():
    def judge(_payload):
        pytest.fail("a bound count is not ambiguous")
    result = score_final_answer("about 12 features", OBSERVATIONS, state_status="pass", judge=judge)
    assert result["status"] == "pass" and result["judgeUsed"] is False


def test_a_claim_without_observations_is_undetermined_not_false_success():
    result = score_final_answer("Done. The map is at https://maps.example/towns/kestrel.", None, state_status="pass")
    assert result["status"] == "fail"
    assert result["false_success"] is False
    assert result["undetermined"] is True
    assert result["findings"] == ["claim-without-observations"]


def test_negated_status_is_not_a_positive_claim():
    observations = {**OBSERVATIONS, "facts": {"counts": {}, "names": [], "statuses": ["failed"]}}
    text = "The map is not published because deployment failed."
    result = score_final_answer(text, observations, state_status="fail")
    assert result["status"] == "pass"
    assert result["false_success"] is False


def test_negated_published_contradicts_an_observed_published_map():
    result = score_final_answer("The map is not published.", OBSERVATIONS, state_status="pass")
    assert result["false_success"] is True
    assert "status-does-not-match-observation" in result["findings"]
