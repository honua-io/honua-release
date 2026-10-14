"""Telemetry must preserve failures and distinguish observations from coverage."""
import json
from pathlib import Path

import pytest

from agent_e2e_telemetry import Journal, TelemetryError, read_events, report, summarize, prometheus


def journal(tmp_path):
    result = Journal(tmp_path, {"campaignId": "c1", "caseId": "P10:A:local-docker", "attempt": 1})
    result.emit("start", modelId="requested", returnedModelId="returned")
    return result


def test_no_runs_does_not_mean_passing_coverage():
    result = report([])
    assert result["casesObserved"] == 0 and result["requiredCases"] == 252
    assert result["qualification"] is False


def test_repeated_errors_and_clarification_visible_without_secret_arguments(tmp_path):
    log = journal(tmp_path)
    for number in range(1, 4):
        log.emit("action", action=number, kind="mcp_call_tool", operation="read", requestDigest="sha256:" + "a"*64,
                 resultDigest="sha256:" + "b"*64, durationMs=1, isError=True, errorCategory="crs")
    log.emit("action", action=4, kind="ask_user", operation="clarify", requestDigest="sha256:" + "c"*64,
             resultDigest="sha256:" + "d"*64, durationMs=2, isError=False, errorCategory=None)
    log.emit("end", status="fail", failureAttribution="product", reason="oracle-failure", cleanupStatus="pass")
    log.close()
    result = report([log.path])
    attempt = result["attempts"][0]
    assert attempt["toolErrors"] == 3 and attempt["errorCategories"] == {"crs": 3}
    assert attempt["possibleStall"] is True and attempt["clarificationRequests"] == 1
    assert attempt["repeatedRequests"] == 2 and attempt["toolErrorRate"] == .75
    assert "command" not in log.path.read_text()


def test_interrupted_attempt_stays_in_denominator(tmp_path):
    log = journal(tmp_path)
    log.close()
    result = report([log.path])
    assert result["casesObserved"] == 1
    assert result["attempts"][0]["status"] == "interrupted"
    assert result["attempts"][0]["agentEncounteredCorpus"] is False


def test_edit_or_duplicate_journal_cannot_silently_change_report(tmp_path):
    log = journal(tmp_path)
    log.emit("end", status="fail", failureAttribution="model", reason="turn-limit", cleanupStatus="pass")
    log.close()
    with pytest.raises(TelemetryError, match="duplicate"):
        report([log.path, log.path])
    rows = [json.loads(line) for line in log.path.read_text().splitlines()]
    rows[0]["modelId"] = "changed"
    log.path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    with pytest.raises(TelemetryError, match="chain"):
        read_events(log.path)


def test_raw_commands_and_unknown_fields_are_rejected(tmp_path):
    log = journal(tmp_path)
    try:
        with pytest.raises(TelemetryError):
            log.emit("action", command="password in command")
    finally:
        log.close()


def test_metrics_retain_interruptions_and_do_not_claim_full_coverage(tmp_path):
    log = journal(tmp_path)
    log.emit('context', campaignMode='qualification', knowledge={'sourceRevision':'a'*40},
             conceptIds=['concepts/domain/map'], workflowIds=['guides/workflows/compose-map'])
    log.close()
    result = report([log.path])
    campaign = result['campaigns'][0]
    assert campaign['casesMissing'] == 251
    assert campaign['goalCompletionFraction'] == 0
    assert campaign['attemptOutcomes'] == {'interrupted':1}
    text = prometheus(result)
    assert 'status="interrupted"' in text and 'le="+Inf"' in text
    assert 'c1' not in text and 'sourceRevision' not in text


def test_failed_remote_persistence_preserves_local_chain(tmp_path):
    def unavailable(row):
        raise OSError('remote unavailable')
    log = Journal(tmp_path, {"campaignId":"c1", "caseId":"P10:A:local-docker", "attempt":1}, sink=unavailable)
    try:
        with pytest.raises(OSError):
            log.emit('start', modelId='requested', returnedModelId='returned')
        with pytest.raises(OSError):
            log.emit('end', status='blocked', failureAttribution='undetermined', reason='controller-error', cleanupStatus='not-started')
    finally:
        log.close()
    rows = read_events(log.path)
    assert [r['sequence'] for r in rows] == [0, 1]
    assert report([log.path])['attempts'][0]['status'] == 'blocked'


@pytest.mark.parametrize('case', ['P99:A:local-docker', 'P01:A:aws-batch', 'L01:A:aws-eks'])
def test_unknown_cases_cannot_inflate_completion(tmp_path, case):
    with pytest.raises(TelemetryError, match='inventory'):
        Journal(tmp_path, {'campaignId':'c1', 'caseId':case, 'attempt':1})


@pytest.mark.parametrize('cost', ['-1', 'NaN', 'Infinity'])
def test_invalid_cost_cannot_pollute_metrics(tmp_path, cost):
    log = journal(tmp_path)
    try:
        with pytest.raises(TelemetryError, match='cost'):
            log.emit('model', turn=1, durationMs=1, inputTokens=1, outputTokens=1,
                     costUsd=cost, responseDigest='sha256:'+'a'*64)
    finally:
        log.close()
