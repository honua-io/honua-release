"""Concrete controller HTTP setup and independent point-query observations.

These adapters operate on an already deployed, independently isolated candidate.
They do not create infrastructure or run model-selected commands. Local Docker
installation, separate approval, fault injection and rendering are
agent_e2e_scenario_install, agent_e2e_scenario_approval, agent_e2e_scenario_fault
and agent_e2e_scenario_render. agent_e2e_scenario_batch refuses local-docker.
Custody changes and a complete 252-case backend are still not ready.
"""
from __future__ import annotations

import copy
from pathlib import Path
import re
from urllib.parse import quote

from agent_e2e_budget import validate as validate_budget
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, verify_corpus
from agent_e2e_query_recipes import point_selection_recipe
from agent_e2e_semantics import project_observation, verify
from agent_e2e_setup_http import SetupHttp
from agent_e2e_scenario_operations import (completed_import, connection_id as canonical_connection,
    discover_tables, publication_request, submit_publication)


def require(value, reason):
    if not value:
        raise PackError(reason)


class ScenarioHttp:
    """Use the producer's live fenced lease, not a second competing writer.

    A backend that uses this class must run inside its trusted controller service
    or share this same DurableExecution instance. Worker identities cannot call
    it. Every HTTP intent and observation is persisted before returning.
    """
    def __init__(self, *, durable, context, endpoint, credential_env, attempt_id,
                 credential_kind='api-key', timeout=300, http_factory=SetupHttp, agent_context=None):
        self.durable, self.context = durable, copy.deepcopy(context)
        self.identity = {'candidateKey': durable.store.candidate, 'attemptId': attempt_id}
        require(context['componentBindingDigest'] == durable.store.candidate,
                'scenario setup differs from durable candidate')
        self.client = http_factory(endpoint, credential_env, identity=self.identity,
            authorize=self.authorize, retain=self.retain, credential_kind=credential_kind, timeout=timeout,
            **({'agent_context': agent_context} if agent_context is not None else {}))

    def authorize(self):
        self.durable._require_live_lease()
        ledger = self.durable.state['ledger']
        validate_budget(ledger)
        require(ledger['candidateKey'] == self.identity['candidateKey'], 'scenario ledger differs from candidate')
        rows = [row for row in ledger['entries'] if row['state'] == 'reserved' and
                all(row.get(k) == self.context[k] for k in ('campaignId', 'caseId', 'attempt'))]
        require({'infrastructure', 'cleanup'} <= {row['kind'] for row in rows},
                'scenario setup requires prepaid attempt infrastructure and cleanup')

    def retain(self, kind, step, record):
        self.authorize()
        require(kind in ('intent', 'response'), 'invalid scenario retention kind')
        key = canonical_digest({**self.identity, 'kind': kind, 'step': step})
        records = self.durable.state.setdefault('scenarioOperations', {})
        require(key not in records, 'scenario operation already retained; reconcile without replay')
        records[key] = copy.deepcopy(record)
        self.durable._persist()
        return {'key': key, 'sha256': canonical_digest(record)}

    def json(self, step, method, path, body=None):
        return self.client.json(step, method, path, body)

    def upload(self, step, raw, *, table, schema, srid):
        return self.client.upload(step, raw, table=table, schema=schema, srid=srid)


def successful(answer, statuses=(200, 201)):
    observation = answer['observation']
    require(observation['httpStatus'] in statuses, 'scenario product HTTP operation failed')
    body = observation['body']
    require(isinstance(body, dict) and body.get('success', True) is not False and 'error' not in body,
            'scenario product response reports failure')
    value = body.get('data', body)
    require(isinstance(value, dict), 'scenario response lacks observed resource object')
    return value


class TownSetup:
    """Seed immutable starting data through existing import/publication APIs.

    This is fixture setup for imported-town/published-town starting states; it
    must never be used to carry out a scenario's requested import or publication.
    Generated table/service names are case-owned, and overwrite is forbidden.
    Actual IDs and schema/table names always come from product responses.
    """
    SOURCES = {'parcels': 'Polygon', 'flood_zones': 'Polygon', 'shelters': 'Point'}

    def __init__(self, http, corpus_manifest):
        self.http = http
        self.manifest_path = Path(corpus_manifest)
        self.manifest = load_json(self.manifest_path)
        verify_corpus(self.manifest_path.parent, self.manifest)
        self.files = {row['path']: row for row in self.manifest['files']}

    def prepare(self, *, variant, namespace, starting_state, connection_id=None):
        require(variant in ('A', 'B', 'C') and re.fullmatch(r'[a-z][a-z0-9_]{0,35}', namespace),
                'invalid case-owned town namespace')
        require(starting_state in ('imported-town', 'published-town'), 'unsupported town starting state')
        if starting_state == 'published-town':
            connection_id = canonical_connection(connection_id)
            observed = successful(self.http.json('observe-connection', 'GET',
                '/api/v1/admin/connections/'+quote(connection_id, safe='')))
            require(observed.get('connectionId', observed.get('id')) == connection_id,
                    'observed connection differs from fixture target')
        bindings = {}
        for name, geometry in self.SOURCES.items():
            relative = f'town-{variant}/{name}.3857.geojson'
            require(relative in self.files, 'town source missing from frozen corpus')
            row = self.files[relative]
            raw = (self.manifest_path.parent/relative).read_bytes()
            require(digest_bytes(raw) == 'sha256:'+row['sha256'], 'town fixture changed before upload')
            table = namespace+'_'+name
            before_tables = None if connection_id is None else discover_tables(
                self.http, connection_id, 'before-tables-'+name.replace('_', '-'))
            imported_answer = self.http.upload('import-'+name.replace('_', '-'), raw, table=table, schema='honua_data', srid=3857)
            imported = completed_import(self.http, imported_answer, table=table,
                feature_count=len(load_json(self.manifest_path.parent/relative)['features']),
                step='complete-'+name.replace('_', '-'), before_tables=before_tables, connection=connection_id)
            physical, schema = imported['table'], imported['schema']
            binding = {'sourcePath': relative, 'sourceDigest': digest_bytes(raw), 'schema': schema,
                       'table': physical, 'importResponse': imported_answer['reference'],
                       'importObservations': imported['references'], 'importJobId': imported['jobId']}
            if starting_state == 'published-town':
                service = namespace+'-'+name.replace('_', '-')
                published = submit_publication(self.http, publication_request(connection=connection_id,
                    schema=schema, table=physical, service=service, geometry_type=geometry, srid=3857),
                    step='publish-'+name.replace('_', '-'))
                require(published['status'] == 'completed',
                        'fixture publication requires actual separate approval; preserve proposal and reconcile')
                binding.update(service=service, layerId=published['layerId'], publicationResponse=published['reference'],
                               publicationOperationId=published['operationInstanceId'])
            bindings[name] = binding
        return {'namespace': namespace, 'variant': variant, 'startingState': starting_state, 'layers': bindings}


def observe_point_selections(observer, binding):
    """Perform three real deterministic checks with raw before/result/after reads.

    The supplied observer uses controller-only read credentials and its own
    durable retention. The return value is partial semantic evidence, never a
    complete 15-check report or a model-scenario receipt.
    """
    require(isinstance(binding, dict) and {'service', 'layerId'} <= set(binding),
            'point probes require observed publication binding')
    checks = []
    for case in ('select-strict', 'select-inclusive', 'select-inclusive-null'):
        recipe = point_selection_recipe(binding['service'], binding['layerId'], case)
        source = recipe['source']['requests'][0]
        before = observer.json(case+'-before', source['method'], source['path'])
        request = recipe['requests'][0]
        result = observer.json(case+'-result', request['method'], request['path'])
        after = observer.json(case+'-after', source['method'], source['path'])
        responses = [row['observation'] for row in (before, result, after)]
        require(all(row['httpStatus'] == 200 for row in responses), 'point probe failed candidate HTTP read')
        projection = {'sourceCount': 1, 'sourceFields': recipe['source']['result'], 'result': recipe['result']}
        observation = project_observation(responses, projection)
        verify(case, observation)
        checks.append({'id': case, 'observation': observation,
                       'rawReferences': [row['reference'] for row in (before, result, after)],
                       'projection': projection})
    return {'producer': 'independent-partial-point-probes', 'checks': checks, 'qualification': False}


def handoff_observations(http):
    """Candidate API reads for the customer handoff. The model transcript is not an input."""
    from agent_e2e_scenario_handoff import observe_handoff
    return observe_handoff(http)
