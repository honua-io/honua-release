"""Telemetry must preserve failures and distinguish observations from coverage."""
import json
from pathlib import Path

import pytest

from agent_e2e_telemetry import Journal, TelemetryError, read_events, report, summarize, prometheus
from agent_e2e_telemetry import atomic_write


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
        log.emit('action-intent', action=number, kind='mcp_call_tool', requestDigest='sha256:'+'a'*64)
        log.emit("action", action=number, kind="mcp_call_tool", operation="read", requestDigest="sha256:" + "a"*64,
                 resultDigest="sha256:" + "b"*64, durationMs=1, isError=True, errorCategory="crs")
    log.emit('action-intent', action=4, kind='ask_user', requestDigest='sha256:'+'c'*64)
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


def test_unresolved_action_intent_survives_interruption_and_is_not_a_success(tmp_path):
    log = journal(tmp_path)
    log.emit('action-intent', action=1, kind='terminal', requestDigest='sha256:'+'a'*64)
    log.close()
    value = report([log.path])
    attempt = value['attempts'][0]
    assert attempt['status'] == 'interrupted'
    assert attempt['actionIntents'] == attempt['actionsUnresolved'] == 1
    assert attempt['actions'] == 0
    metrics = prometheus(value)
    assert 'honua_agent_action_intents_total{target="local-docker",mode="unbound"} 1' in metrics
    assert 'honua_agent_actions_unresolved{target="local-docker",mode="unbound"} 1' in metrics
    assert 'a'*64 not in metrics


@pytest.mark.parametrize('mutation', ['unresolved-pass', 'missing-intent', 'changed-request', 'duplicate-observation'])
def test_missing_or_mismatched_action_telemetry_cannot_become_success(tmp_path, mutation):
    log = journal(tmp_path)
    if mutation != 'missing-intent':
        log.emit('action-intent', action=1, kind='terminal', requestDigest='sha256:'+'a'*64)
    if mutation != 'unresolved-pass':
        digest = 'sha256:'+('b' if mutation == 'changed-request' else 'a')*64
        for _ in range(2 if mutation == 'duplicate-observation' else 1):
            log.emit('action', action=1, kind='terminal', operation='read', requestDigest=digest,
                     resultDigest='sha256:'+'c'*64, durationMs=1, isError=False, errorCategory=None)
    log.emit('end', status='pass', failureAttribution=None, reason='verified', cleanupStatus='pass')
    log.close()
    with pytest.raises(TelemetryError, match='dispatch'):
        report([log.path])


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


def test_correlations_survive_reports_but_never_become_metric_labels(tmp_path):
    log = journal(tmp_path)
    log.emit('provider-request', turn=1, requestId='provider-request-123')
    log.emit('action-intent', action=1, kind='mcp_call_tool', requestDigest='sha256:'+'d'*64)
    log.emit('runtime-trace', action=1, requestId='server-request-456',
             traceId='a'*32, spanId='b'*16, evidenceDigest='sha256:'+'c'*64)
    log.close()
    value = report([log.path])
    attempt = value['attempts'][0]
    assert attempt['providerRequests'][0]['requestId'] == 'provider-request-123'
    assert attempt['runtimeTraces'][0]['evidenceDigest'] == 'sha256:'+'c'*64
    metrics = prometheus(value)
    assert 'request-123' not in metrics and 'request-456' not in metrics and 'a'*32 not in metrics


@pytest.mark.parametrize('change', [{'traceId':'0'*32}, {'spanId':'bad'}, {'action':0},
                                  {'requestId':'Bearer private-value'}, {'evidenceDigest':'unbound'}])
def test_malformed_or_sensitive_trace_fields_are_rejected(tmp_path, change):
    log = journal(tmp_path)
    values = {'action':1, 'requestId':'server-123', 'traceId':'a'*32,
              'spanId':'b'*16, 'evidenceDigest':'sha256:'+'c'*64, **change}
    try:
        with pytest.raises(TelemetryError):
            log.emit('runtime-trace', **values)
    finally:
        log.close()


def test_failed_atomic_publication_keeps_previous_metrics(tmp_path, monkeypatch):
    path = tmp_path/'metrics.prom'
    path.write_bytes(b'previous complete metrics\n')
    def fail_replace(*args):
        raise OSError('replace failed')
    monkeypatch.setattr('agent_e2e_telemetry.os.replace', fail_replace)
    with pytest.raises(OSError):
        atomic_write(path, b'new metrics\n')
    assert path.read_bytes() == b'previous complete metrics\n'
    assert sorted(p.name for p in tmp_path.iterdir()) == ['metrics.prom']
