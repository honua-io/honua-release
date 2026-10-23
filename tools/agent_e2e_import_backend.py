"""Native model-driven P04/P05 import development sessions.

The controller supplies immutable source files and an empty isolated destination.
Only the model creates native tasks, transfers bytes and submits governed jobs.
Independent original task/job GETs and PostGIS rows drive the existing corpus
predicates. Unknown jobs, unsupported CSV diagnostics, SQL owner transfer or
cleanup keep failures/liabilities; no setup import or guessed cost can qualify.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import os

from agent_e2e_import_database import ImportedFixtureDatabase
from agent_e2e_import_observer import DatasetImportObserver, SqlImportedDataset, SUBMIT
from agent_e2e_mcp import validate_agent_context
from agent_e2e_native_costs import reserve_overhead
from agent_e2e_p10_backend import Session as WorkerSession, identity
from agent_e2e_pack import PackError, canonical_digest, digest_bytes, load_json, selected_files
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_studio_backend import Session as PublicationSession
import agent_e2e_gate as gate

SESSIONS = {}


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def primary_sources(case, corpus_root, destinations, sources):
    guide_path = 'town-'+case['variant']+'/input-guide.json'
    guide = load_json(gate.safe_file(corpus_root, guide_path))
    required = ('parcels', 'flood_zones') if case['scenarioId'] == 'P04' else ('shelters',)
    primary = [guide['primaryFiles'][name] for name in required]
    require(len(destinations) == len(primary) and {row['fileName'] for row in destinations} == set(primary),
            'native import profile must cover every authored primary dataset from the original input guide')
    by_path = {row['resourceId']: row['content'] for row in sources}
    for destination in destinations:
        original = by_path.get('town-'+case['variant']+'/'+destination['fileName'])
        require(original is not None and original['sha256'] == destination['sourceSha256'] and
                original['bytes'] == destination['totalBytes'], 'native import destination changed primary source bytes')


class Session(WorkerSession):
    PROFILE = 'dataset-import-native'
    CASE_PATTERN = r'P0[45]:[AB]:aws-ecs-redis-on'
    CONFIG_FIELDS = {'http', 'database', 'deployment', 'attempts', 'accounting', 'import', 'ownershipTransfer'}
    OPTIONAL_FIELDS = set()
    DATABASE = ImportedFixtureDatabase
    cleanup_fixture = PublicationSession.cleanup_fixture

    def validate_observer(self, http, secrets):
        config = self.config['import']
        require(set(config) == {'workspaceId', 'ownerPrincipalId', 'tenantId', 'targetId', 'destinations',
            'requesterCredentialEnv', 'requesterCredentialKind', 'requesterAgentContext'} and all(secrets) and
            secrets[0] != secrets[1] and os.environ.get(config['requesterCredentialEnv']) and
            os.environ[config['requesterCredentialEnv']] != secrets[0] and config['requesterCredentialKind'] in ('api-key', 'bearer') and
            validate_agent_context(config['requesterAgentContext']) is not None and
            config['requesterAgentContext']['workspaceId'] == config['workspaceId'],
            'native import requires separate setup/task credentials and the actual captured workspace/turn')

    def source(self):
        rows = selected_files(self.pack, self.context['caseId'], load_json(self.corpus_path))
        snapshots = []
        for row in rows:
            path = gate.safe_file(self.corpus_path.parent, row['path'])
            raw = path.read_bytes()
            require(digest_bytes(raw) == 'sha256:'+row['sha256'] and len(raw) == row['bytes'], 'original selected import source bytes changed')
            snapshots.append({'resourceId': row['path'], 'content': {'sha256': digest_bytes(raw)[7:], 'bytes': len(raw)}})
        return snapshots

    def observer_for_recovery(self):
        config = self.config['import']
        baseline = self.durable.state.get('fixtureBaselines', {}).get(self.key)
        binding = self.value('import-binding')
        require(baseline is not None and binding is not None, 'native import original source/destination baseline unresolved')
        http = ScenarioHttp(durable=self.durable, context=self.context, endpoint=self.config['http']['endpoint'],
            credential_env=config['requesterCredentialEnv'], credential_kind=config['requesterCredentialKind'],
            attempt_id=self.attempt_id+'-import-observer', agent_context=config['requesterAgentContext'])
        self.observer = DatasetImportObserver(http=http, binding=binding,
            sql=SqlImportedDataset(self.database, baseline=baseline), retain=self.retain)
        return self.observer

    def prepare(self):
        require(self.phase('prepare-start') is None, 'native import prepare already started; reconcile without replay')
        self.retain('prepare-start', {'namespace': self.namespace, 'attemptId': self.attempt_id,
            'startedAt': datetime.now(timezone.utc).isoformat()})
        self.retain('overhead-admission', reserve_overhead(self.durable, self.context,
            self.config['accounting']['overheadCoverage']))
        self.observe_deployment()
        baseline = self.database.baseline()
        destinations = self.config['import']['destinations']
        for destination in destinations:
            require(not any(row['schema'] == destination['schema'] and row['name'] == destination['table']
                    for row in baseline['snapshot']['relations']), 'native import destination already contains shared/preexisting data')
        self.durable.retain_fixture_baseline(self.context, baseline)
        binding = {key: copy.deepcopy(self.config['import'][key]) for key in
                   ('workspaceId', 'ownerPrincipalId', 'tenantId', 'targetId', 'destinations')}
        binding['notBefore'] = self.value('prepare-start')['startedAt']
        self.retain('import-binding', binding)
        self.observer_for_recovery()
        sources = self.source()
        source_digests = {row['content']['sha256'] for row in sources}
        require(all(destination['sourceSha256'] in source_digests for destination in destinations),
                'native import source declaration is outside selected original case corpus')
        primary_sources(self.case, self.corpus_path.parent, destinations, sources)
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']['source-content.unchanged']
        gate.oracle_predicates.verify('source-content.unchanged', {'profile': 'honua.protected-content/v1',
            'before': sources, 'after': sources}, expected)
        self.retain('source-before', sources)
        return self.start_worker(public_target=self.config['http']['endpoint']+'; native workspace '+binding['workspaceId']+
            '; isolated import destinations: '+', '.join(row['schema']+'.'+row['table']+' for '+row['fileName'] for row in destinations))

    def action(self, payload, timeout):
        require(self.value('public-inputs') is not None, 'native import worker public inputs are not independently verified')
        self.open_worker()
        action, dispatch = payload['action'], payload['dispatch']
        self.retain('model-action-'+str(dispatch['ordinal']), {'action': action, 'dispatch': dispatch})
        observed = self.execute_action(action, dispatch, timeout)
        self.retain('model-result-'+str(dispatch['ordinal']), observed)
        observer = self.observer_for_recovery()
        if observer.eligible(action):
            try: selected = observer.selected(action, dispatch, observed)
            except (PackError, ValueError, KeyError, TypeError) as error:
                self.retain('import-rejected-'+str(dispatch['ordinal']), {'actionId': dispatch['actionId'], 'errorType': type(error).__name__})
                return observed
            self.retain('import-ordinal-'+str(dispatch['ordinal']), selected)
        return observed

    def verify(self):
        observer = self.observer_for_recovery()
        selected = sorted((row for row in self.durable.state.get('scenarioOperations', {}).values() if
            row.get('producer') == self.PROFILE and row.get('identity') == identity(self.context) and
            row.get('phase', '').startswith('import-ordinal-')), key=lambda row: int(row['phase'].rsplit('-', 1)[1]))
        tasks = {}
        for row in selected:
            if row['value']['tool'] == SUBMIT:
                tasks[row['value']['task']['taskId']] = row['value']
        observations = {assertion: None for assertion in self.case['assertions']}
        observations['source-content.unchanged'] = {'profile': 'honua.protected-content/v1',
            'before': self.value('source-before'), 'after': self.source()}
        completed, by_destination = [], {}
        for index, selection in enumerate(tasks.values()):
            actual = observer.observe(selection, step='completed-import-'+str(index))
            self.retain('import-status-'+str(index), actual)
            if actual['dataset'] is not None:
                destination = selection['task']['destination']
                key = (destination['schema'], destination['table'])
                if key in by_destination: by_destination[key] = None
                else: by_destination[key] = actual
                completed.append(actual)
        wanted = [(row['schema'], row['table']) for row in self.config['import']['destinations']]
        if len(tasks) == len(wanted) and set(by_destination) == set(wanted) and all(by_destination.values()):
            ordered = [by_destination[key] for key in wanted]
            srids = {row['features.exact']['srid'] for row in ordered}
            if len(srids) == 1:
                features = sorted((feature for row in ordered for feature in row['features.exact']['features']), key=lambda row: row['id'])
                observations['features.exact'] = {'srid': next(iter(srids)), 'features': features}
                original_srids = [row['geometry.crs']['sourceSrid'] for row in ordered]
                observations['geometry.crs'] = {'sourceSrid': original_srids[0] if len(original_srids) == 1 else original_srids,
                    'returnedSrid': next(iter(srids)), 'coordinateDigest': canonical_digest([
                        {'sourceId': row['id'], 'geometry': row['geometry']} for row in features])[7:]}
            self.retain('completed-imports', completed)
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']
        failed = []
        for assertion in self.case['assertions']:
            try: gate.oracle_predicates.verify(assertion, observations[assertion], expected[assertion])
            except (ValueError, TypeError, PackError): failed.append(assertion)
        result = {'status': 'fail' if failed else 'pass', 'failedAssertions': failed, 'observations': observations,
            'unsupportedAssertions': [assertion for assertion in self.case['assertions'] if assertion not in
                {'features.exact', 'geometry.crs', 'source-content.unchanged'}]}
        self.retain('verification', result)
        return {key: result[key] for key in ('status', 'failedAssertions')}

    def prepare_cleanup_ownership(self):
        require(self.value('worker-stopped') is not None, 'native import ownership cannot change before task worker is independently stopped')
        observations = self.value('completed-imports')
        require(observations, 'native import output remains unresolved; retain all jobs and cleanup liabilities')
        baseline = self.durable.state['fixtureBaselines'][self.key]
        request = self.database.observe_import_tables(baseline, observations)
        self.retain('observed-source-owners', request)
        if any(table['owner'] != request['expectedCleanupRole'] for table in request['tables']):
            from hosted_pilot.fixture_handoff import transfer_ownership
            receipt = transfer_ownership(request, durable=self.durable, context=self.context,
                configuration=self.config['ownershipTransfer'], timeout=300)
            self.retain('ownership-transfer-receipt', receipt)
        ownership = self.database.ownership_imports(baseline, request)
        self.durable.retain_fixture_ownership(self.context, ownership)


def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout):
    require(operation != 'export_campaign', 'native import development cannot export complete 252-case campaign evidence')
    context = {**campaign, **identity(payload)} if operation == 'export_attempt' else payload
    key = (id(durable), canonical_digest(identity(context)))
    if key not in SESSIONS:
        SESSIONS[key] = Session(durable=durable, campaign=campaign, bundle=bundle, configuration=configuration, context=context)
    session = SESSIONS[key]
    if operation == 'prepare': return session.prepare()
    if operation == 'agent_action': return session.action(payload, timeout)
    if operation == 'verify': return session.verify()
    if operation == 'cleanup': return session.cleanup()
    if operation == 'export_attempt': return session.export_attempt(payload)
    raise PackError('unsupported native dataset import operation')
