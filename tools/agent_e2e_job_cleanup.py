"""Retain native GP cleanup obligations after the original worker has stopped.

The current product's completed-job DELETE returns conflict. A missing public
GET, a cancelled job, and a configured TTL do not prove that the job, its result
package and its artifacts were retired. This development adapter therefore
blocks cleanup/settlement for every dispatched execute until a supported,
independently observed retirement path exists. It never deletes Redis keys or
infrastructure and never invents an empty resource inventory.
"""
from __future__ import annotations

from agent_e2e_job_observer import JobObserver
from agent_e2e_p10_backend import identity
from agent_e2e_scenario_backend import successful


def inventory(session):
    records = [row for row in session.durable.state.get('scenarioOperations', {}).values()
               if row.get('producer') == session.PROFILE and row.get('identity') == identity(session.context)]
    actions = [row['value'] for row in records if row.get('phase', '').startswith('model-action-') and
               (JobObserver.eligible(row['value']['action']) or row['value']['action'].get('name') == 'terminal')]
    selections = session.job_selections()
    obligations = []
    # Selections are retained only after the original MCP result joins an
    # independent native GET, including requester and accepted fingerprint.
    for selected in selections:
        obligation = {'jobId': selected['jobId'], 'actionId': selected['actionId'],
                      'ordinal': selected['ordinal'], 'retirementObserved': False}
        try:
            observer = session.job_observer()
            job, reference = observer.observe(selected, step='gp-cleanup-'+str(selected['ordinal']))
            obligation.update(status=job['status'], jobReference=reference)
            answer = observer.http.json('gp-cleanup-artifacts-'+str(selected['ordinal']), 'GET',
                '/api/v1/admin/jobs/'+selected['jobId']+'/artifacts?limit=200')
            obligation['artifactsReference'] = answer['reference']
            artifacts = successful(answer)
            if artifacts.get('jobId') != selected['jobId'] or not isinstance(artifacts.get('items'), list):
                raise ValueError('native cleanup artifact inventory does not join its job')
            obligation['artifacts'] = artifacts['items']
            obligation['artifactInventoryComplete'] = 'nextCursor' in artifacts and artifacts['nextCursor'] is None
        except Exception as error:
            # A denied/missing/malformed read preserves the original obligation.
            # It does not establish absence or allow settlement of its hold.
            obligation['observationErrorType'] = type(error).__name__
        obligations.append(obligation)
    selected_ordinals = {row['ordinal'] for row in selections}
    # Terminal commands can submit native requests too. Their success/failure
    # text is not an exhaustive effect inventory, even after the worker stops.
    unresolved = [{'actionId': row['dispatch']['actionId'], 'ordinal': row['dispatch']['ordinal'],
                   'actionKind': row['action']['name']}
                  for row in actions if row['dispatch']['ordinal'] not in selected_ordinals]
    pending = bool(actions or selections)
    return {'status': 'blocked' if pending else 'pass',
            'reason': 'native-job-retirement-unproven' if pending else 'no-managed-execution-dispatched',
            'resourceIds': sorted({'honua://jobs/'+row['jobId'] for row in selections}),
            'jobs': obligations, 'unresolvedExecutions': unresolved}
