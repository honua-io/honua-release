"""Shared native model authoring runtime for Studio map/dashboard journeys.

Only source fixtures are prepared for new-map P08. The model must actually
create its exact scoped item, compose and save it. Saved-state cases consume
the retained prefreeze native fixture. The same dispatched model actions and
independent GET/SQL observers are used across authoring cases; missing render,
job, restart, version-restore or fresh-session evidence stays nonpassing.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
import re

from agent_e2e_mcp import validate_agent_context
from agent_e2e_native_costs import reserve_overhead
from agent_e2e_p10_backend import Session as WorkerSession, identity
from agent_e2e_pack import PackError, canonical_digest, load_json
from agent_e2e_query_projection import normalize
from agent_e2e_scenario_backend import ScenarioHttp
from agent_e2e_semantics import pointer
from agent_e2e_studio_backend import Session as PublicationSession
from agent_e2e_studio_composition_observer import StudioCompositionObserver, CREATE, SAVE
from agent_e2e_studio_database import StudioFixtureDatabase
from agent_e2e_studio_observer import guid
import agent_e2e_gate as gate

SESSIONS = {}


def require(condition, reason):
    if not condition:
        raise PackError(reason)


class Session(WorkerSession):
    PROFILE = 'studio-authoring-native'
    CASE_PATTERN = r'P(?:08|09|11|12|13):[AB]:aws-ecs-redis-on'
    CONFIG_FIELDS = {'http', 'database', 'deployment', 'attempts', 'accounting', 'studio', 'sources'}
    OPTIONAL_FIELDS = {'rendering', 'managedJob', 'styling'}
    DATABASE = StudioFixtureDatabase
    initial_fixture = PublicationSession.initial_fixture

    def cleanup_fixture(self, ownership):
        # SQL teardown still runs after worker stop, even when a native job needs
        # separate retirement. Its receipt cannot stand in for that retirement.
        sql = PublicationSession.cleanup_fixture(self, ownership)
        if getattr(self, 'config', {}).get('managedJob') is not None:
            self.retain('managed-job-sql-cleanup', sql)
        return sql

    def cleanup_resources(self):
        # A job may have been dispatched before any Studio draft existed.
        # Inventory runs after the worker-stop and SQL attempts independently.
        if getattr(self, 'config', {}).get('managedJob') is None:
            return
        from agent_e2e_job_cleanup import inventory
        remaining = inventory(self)
        self.retain('managed-job-cleanup', remaining)
        require(remaining['status'] == 'pass',
                'managed job retirement is unresolved; retain original cleanup liabilities')

    def validate_observer(self, http, secrets):
        studio = self.config['studio']
        require(set(studio) == {'itemId', 'namespace', 'ownerPrincipalId', 'family', 'bootstrapFixture',
                'requesterCredentialEnv', 'requesterCredentialKind', 'requesterAgentContext'} and
                studio['family'] == 'map' and all(secrets) and secrets[0] != secrets[1] and
                os.environ.get(studio['requesterCredentialEnv']) and os.environ[studio['requesterCredentialEnv']] != secrets[0] and
                validate_agent_context(studio['requesterAgentContext']) is not None,
                'Studio authoring lacks exact item scope, native task context and separate setup/task identities')
        guid(studio['itemId'])
        require(studio['requesterCredentialKind'] in ('api-key', 'bearer') and
                re.fullmatch(r'ae_[a-z0-9_]{3,35}', studio['namespace']) and studio['ownerPrincipalId'], 'invalid Studio authoring target')
        sources = self.config['sources']
        require(isinstance(sources, list) and 1 <= len(sources) <= 16 and
                len({row.get('resourceId') for row in sources}) == len(sources), 'missing/ambiguous independent source-readback inventory')
        for source in sources:
            require(set(source) == {'resourceId', 'path', 'pointer', 'normalizer'} and
                    isinstance(source['resourceId'], str) and source['resourceId'] and
                    isinstance(source['path'], str) and source['path'].startswith('/') and
                    isinstance(source['pointer'], str) and source['pointer'].startswith('/') and
                    source['normalizer'] in (None, 'feature-server-points/v1'), 'unreviewed source-content readback profile')
        if 'rendering' in self.config:
            from agent_e2e_studio_render_backend import validate
            declarations = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']].get('nativeBindings', {})
            validate(self.config['rendering'], studio=studio, declarations=declarations,
                     candidate=self.context['componentBindingDigest'])
        if 'managedJob' in self.config:
            from agent_e2e_job_observer import PROCESS
            job = self.config['managedJob']
            require(self.case['scenarioId'] == 'P11' and isinstance(job, dict) and
                    set(job) == {'processId', 'requesterPrincipalId', 'mapLayerId'} and job['processId'] == PROCESS and
                    isinstance(job['requesterPrincipalId'], str) and 0 < len(job['requesterPrincipalId']) <= 512 and
                    isinstance(job['mapLayerId'], str) and 0 < len(job['mapLayerId']) <= 200,
                    'only the reviewed native buffer process/requester/map layer can join P11')
        if 'styling' in self.config:
            from agent_e2e_studio_style_observer import validate
            require(self.case['scenarioId'] == 'P09', 'style observation belongs to the authored P09 case')
            validate(self.config['styling'])
        self.preflight_job_binding()

    def preflight_job_binding(self):
        """Refuse missing or inconsistent frozen job scope before paid effects."""
        from agent_e2e_native_job_bindings import PROFILE, contract
        expected = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]
        declarations = expected.get('nativeBindings', {})
        matches = [(name, value) for name, value in declarations.items() if value.get('profile') == PROFILE]
        job = self.config.get('managedJob')
        if job is None:
            require(not matches, 'frozen managed job requires its original execution configuration')
            return expected
        require(self.case['scenarioId'] == 'P11' and len(matches) == 1,
                'managed execution requires exactly one frozen job binding before worker launch')
        name, declaration = matches[0]
        contract(declaration)
        require(declaration['relationships'] == {
                    'processId': job['processId'], 'requesterPrincipalId': job['requesterPrincipalId']} and
                str(self.context['attempt']) in declaration['observers'],
                'managed execution differs from its frozen process/requester/attempt')
        http = self.config['http']
        require(declaration['observers'][str(self.context['attempt'])] == {
                    'endpoint': http['endpoint'].rstrip('/'),
                    'attemptId': self.attempt_id+'-observer',
                    'credentialKind': http['credentialKind']},
                'managed job observer differs from its frozen endpoint/attempt/credential')
        assertions = expected['assertions']
        composition = assertions.get('map.composition', {})
        require({'job.completed', 'buffer.metric', 'map.composition'} <= set(assertions) and
                assertions['job.completed'].get('resourceId') == {'$native': name+'.jobId'} and
                job['mapLayerId'] in composition.get('orderedLayerIds', []) and
                composition.get('visibility', {}).get(job['mapLayerId']) is True,
                'managed execution lacks frozen lifecycle, metric or mapped-output expectations')
        return expected

    def source(self, *, step):
        snapshots, references = [], []
        http = self.http('observer')
        for index, source in enumerate(self.config['sources']):
            answer = http.json(step+'-'+str(index), 'GET', source['path'])
            require(answer['observation']['httpStatus'] == 200, 'cannot independently observe exact authoring source content')
            content = pointer(answer['observation'], source['pointer'])
            if source['normalizer'] is not None:
                content = normalize(source['normalizer'], content)
            require(isinstance(content, dict) and content, 'source readback has no complete protected content')
            snapshots.append({'resourceId': source['resourceId'], 'content': copy.deepcopy(content)})
            references.append(answer['reference'])
        return {'snapshots': snapshots, 'nativeReferences': references}

    def observer_for_recovery(self):
        binding = self.value('authoring-binding')
        require(binding is not None, 'Studio authoring initial native scope is unresolved')
        self.observer = StudioCompositionObserver(http=self.http('observer'), binding=binding, retain=self.retain)
        return self.observer

    def job_observer(self):
        from agent_e2e_job_observer import JobObserver
        binding = self.value('job-binding')
        require(binding is not None, 'managed job initial requester scope is unresolved')
        return JobObserver(http=self.http('observer'), binding=binding, retain=self.retain)

    def job_selections(self):
        selections = {}
        for row in self.durable.state.get('scenarioOperations', {}).values():
            if (row.get('producer') != self.PROFILE or row.get('identity') != identity(self.context) or
                    not row.get('phase', '').startswith(('job-selected-', 'job-ordinal-'))):
                continue
            selected = row['value']
            previous = selections.get(selected['ordinal'])
            require(previous is None or canonical_digest(previous) == canonical_digest(selected),
                    'durable job selection copies disagree; retain original evidence')
            selections[selected['ordinal']] = selected
        return sorted(selections.values(), key=lambda row: row['ordinal'])

    def observe_job_action(self, action, dispatch, observed):
        if getattr(self, 'config', {}).get('managedJob') is None:
            return
        observer = self.job_observer()
        if observer.eligible(action):
            try:
                observer.selected(action, dispatch, observed)
            except (PackError, ValueError, TypeError, KeyError) as error:
                self.retain('job-rejected-'+str(dispatch['ordinal']), {'errorType': type(error).__name__, 'actionId': dispatch['actionId']})
        else:
            selected = self.job_selections()
            if len(selected) == 1:
                try:
                    actual, reference = observer.observe(selected[0], step='gp-progress-'+dispatch['actionId'][:32])
                    self.retain('job-observation-'+str(dispatch['ordinal']), {'ordinal': dispatch['ordinal'],
                        'jobId': selected[0]['jobId'], 'resource': actual, 'reference': reference})
                except (PackError, ValueError, TypeError, KeyError) as error:
                    self.retain('job-observation-failed-'+str(dispatch['ordinal']), {'errorType': type(error).__name__})

    def verify_job(self, declarations, native_context):
        from agent_e2e_native_job_bindings import PROFILE, retain, observation
        selected = self.job_selections()
        require(len(selected) == 1, 'native buffer execution is absent or ambiguous; retain every original job')
        selected = selected[0]
        result = self.job_observer().results(selected, step='gp-final')
        native_context['completedAt'] = datetime.now(timezone.utc).isoformat()
        reads = sorted((row['value'] for row in self.durable.state.get('scenarioOperations', {}).values() if
            row.get('producer') == self.PROFILE and row.get('identity') == identity(self.context) and
            row.get('phase', '').startswith('job-observation-') and not row['phase'].startswith('job-observation-failed-')),
            key=lambda row: row['ordinal'])
        require(all(row['jobId'] == selected['jobId'] and row['ordinal'] > selected['ordinal'] for row in reads),
                'job lifecycle observations belong to another execution')
        references = [selected['initial']['reference'], *[row['reference'] for row in reads], result['nativeReferences']['completed']]
        matches = [(name, value) for name, value in declarations.items() if value.get('profile') == PROFILE]
        require(len(matches) == 1, 'managed execution lacks its exact frozen native binding')
        name, declaration = matches[0]
        report = retain(self.durable, self.context, name=name, declaration=declaration, responses=references,
            selection=selected, started_at=native_context['startedAt'], completed_at=native_context['completedAt'])
        self.retain('job-final', {'selection': selected, **result})
        return result, name, report, observation(report, declaration, native_context)

    def prepare(self):
        expected_case = self.preflight_job_binding()
        require(self.phase('prepare-start') is None, 'Studio authoring prepare already started; reconcile without replay')
        self.retain('prepare-start', {'namespace': self.namespace, 'attemptId': self.attempt_id,
            'startedAt': datetime.now(timezone.utc).isoformat()})
        self.retain('overhead-admission', reserve_overhead(self.durable, self.context,
            self.config['accounting']['overheadCoverage']))
        self.observe_deployment()
        studio = self.config['studio']
        self.namespace = studio['namespace']
        if self.case['scenarioId'] in ('P08', 'P11'):
            require(studio['bootstrapFixture'] is None, 'new-map case cannot substitute a prepared map for model creation')
            baseline = self.database.baseline()
            require(studio['itemId'] not in json.dumps(baseline['snapshot'], sort_keys=True).lower() and
                    not any(row['workspace_id'] == self.namespace for row in baseline['snapshot']['studio']['studio_content_items']),
                    'new model-authored item/workspace already exists; no overwrite or inferred reuse')
            self.durable.retain_fixture_baseline(self.context, baseline)
        else:
            initial = self.initial_fixture()
            require(all(initial['fixture'].get(field) == studio[configured] for field, configured in
                    (('itemId', 'itemId'), ('namespace', 'namespace'), ('ownerPrincipalId', 'ownerPrincipalId'))),
                    'saved authoring fixture differs from exact configured task item')
            ownership = self.database.ownership(initial['baseline'], initial['fixture'])
            require(ownership == initial['ownership'], 'saved authoring fixture changed before task worker launch')
            self.durable.retain_fixture_baseline(self.context, initial['baseline'])
            self.durable.retain_fixture_ownership(self.context, ownership)
            self.retain('initial-fixture', {'reference': studio['bootstrapFixture'], 'fixture': initial['fixture']})
            if 'styling' in self.config:
                from agent_e2e_studio_style_observer import preflight
                preflight(config=self.config['styling'], initial=initial['fixture']['initialVersion'],
                    expected=expected_case['assertions']['map.style'], item_id=studio['itemId'])
        binding = {key: studio[key] for key in ('itemId', 'namespace', 'ownerPrincipalId', 'family')}
        binding['notBefore'] = self.value('prepare-start')['startedAt']
        self.retain('authoring-binding', binding)
        for declaration in expected_case.get('nativeBindings', {}).values():
            from agent_e2e_native_authoring_bindings import PROFILE
            relations = declaration.get('relationships', {})
            if declaration.get('profile') == 'honua.managed-job-bindings/v1':
                job = self.config.get('managedJob')
                require(self.case['scenarioId'] == 'P11' and job is not None and relations == {
                    'processId': job['processId'], 'requesterPrincipalId': job['requesterPrincipalId']} and
                    job['mapLayerId'] in expected_case['assertions']['map.composition']['orderedLayerIds'] and
                    expected_case['assertions']['map.composition']['visibility'].get(job['mapLayerId']) is True,
                    'frozen managed-job relationship differs from original process/requester/mapped output')
                self.retain('job-binding', {'requesterPrincipalId': job['requesterPrincipalId'], 'notBefore': binding['notBefore']})
                continue
            starting = None if self.case['scenarioId'] in ('P08', 'P11') else self.value('initial-fixture')['fixture']['versionId']
            require(declaration.get('profile') == PROFILE and all(relations.get(key) == binding[key]
                    for key in ('itemId', 'family', 'namespace', 'ownerPrincipalId')) and relations.get('startingVersionId') == starting,
                    'frozen saved-version relationship differs from original authoring starting scope')
        source = self.source(step='prepared-source')
        wanted = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]['assertions']['source-content.unchanged']
        gate.oracle_predicates.verify('source-content.unchanged', {'profile': 'honua.protected-content/v1',
            'before': source['snapshots'], 'after': source['snapshots']}, wanted)
        self.retain('source-before', source)
        return self.start_worker(public_target=self.config['http']['endpoint']+'; Studio '+studio['family']+' item '+studio['itemId']+
            ', workspace/package '+self.namespace+'; available source datasets: '+', '.join(row['resourceId'] for row in self.config['sources']))

    def action(self, payload, timeout):
        require(self.value('public-inputs') is not None, 'Studio authoring worker public inputs are not independently verified')
        self.open_worker()
        action, dispatch = payload['action'], payload['dispatch']
        self.retain('model-action-'+str(dispatch['ordinal']), {'action': action, 'dispatch': dispatch})
        observed = self.execute_action(action, dispatch, timeout)
        self.retain('model-result-'+str(dispatch['ordinal']), observed)
        self.observe_job_action(action, dispatch, observed)
        observer = self.observer_for_recovery()
        if observer.eligible(action):
            try:
                selected = observer.selected(action, dispatch, observed)
            except (PackError, ValueError, TypeError, KeyError) as error:
                # A generation conflict or a failed model mutation stays visible
                # to the model. It never becomes a fabricated successful state.
                self.retain('authoring-rejected-'+str(dispatch['ordinal']), {'errorType': type(error).__name__, 'actionId': dispatch['actionId']})
                return observed
            if selected['tool'] == CREATE:
                require(self.case['scenarioId'] in ('P08', 'P11') and self.key not in self.durable.state.get('fixtureOwnership', {}),
                        'model created a duplicate draft or replaced a saved starting fixture')
                baseline = self.durable.state['fixtureBaselines'][self.key]
                studio = self.config['studio']
                ownership = self.database.ownership_created_draft(baseline, selected['resource'], item_id=studio['itemId'],
                    namespace=studio['namespace'], owner_id=studio['ownerPrincipalId'])
                self.durable.retain_fixture_ownership(self.context, ownership)
            self.retain('authoring-ordinal-'+str(dispatch['ordinal']), selected)
        return observed

    def verify(self):
        observer = self.observer_for_recovery()
        selections = sorted((row for row in self.durable.state.get('scenarioOperations', {}).values() if
            row.get('producer') == self.PROFILE and row.get('identity') == identity(self.context) and
            row.get('phase', '').startswith('authoring-ordinal-')), key=lambda row: int(row['phase'].rsplit('-', 1)[1]))
        saves = [row['value'] for row in selections if row['value']['tool'] == SAVE]
        after, before = self.source(step='completed-source'), self.value('source-before')
        observations = {assertion: None for assertion in self.case['assertions']}
        observations['source-content.unchanged'] = {'profile': 'honua.protected-content/v1',
            'before': before['snapshots'], 'after': after['snapshots']}
        expected_case = load_json(self.bundle/'expectations.json')['cases'][self.context['caseId']]
        declarations, reports = expected_case.get('nativeBindings', {}), {}
        completed = datetime.now(timezone.utc).isoformat()
        native_context = {**self.context, 'startedAt': self.value('prepare-start')['startedAt'] if self.value('prepare-start') else '',
            'completedAt': completed, 'modelActions': [row for row in self.durable.state.get('journals', {}).get(self.key, [])
                                                       if row.get('event') == 'action']}
        job_result = None
        if getattr(self, 'config', {}).get('managedJob') is not None:
            try:
                job_result, name, report, lifecycle = self.verify_job(declarations, native_context)
                completed = native_context['completedAt']
                reports[name] = report
                observations['job.completed'] = lifecycle
                observations['buffer.metric'] = job_result['buffer']
            except (PackError, ValueError, TypeError, KeyError) as error:
                self.retain('job-verification-failed', {'errorType': type(error).__name__})
        if saves:
            saved, version_ref = observer.version(saves[-1]['resource']['versionId'], step='completed-model-version')
            current, current_ref = observer.current(step='completed-model-current')
            draft, draft_ref = observer.draft(saved['sourceDraftId'], step='completed-model-draft')
            if (saved == saves[-1]['resource'] and current.get('currentVersionId') == saved['versionId'] and
                    current.get('publishedVersionId') is None and saved['envelope'] == draft['envelope']):
                if 'map.composition' in observations:
                    try:
                        if getattr(self, 'config', {}).get('managedJob') is not None:
                            from agent_e2e_job_map import join
                            require(job_result is not None, 'saved map cannot stand in for missing native job output')
                            joined = join(saved, job_result['document'], layer_id=self.config['managedJob']['mapLayerId'])
                            self.retain('job-saved-map', {'savedVersionId': saved['versionId'], **joined})
                        observations['map.composition'] = observer.map_composition(saved)
                    except (PackError, TypeError, KeyError): pass
                if 'state.saved' in observations: observations['state.saved'] = observer.saved(saved)
                styling = getattr(self, 'config', {}).get('styling')
                if 'map.style' in observations and styling is not None:
                    try:
                        from agent_e2e_studio_style_observer import observe
                        styled = observe(observer, config=styling,
                            initial=self.value('initial-fixture')['fixture']['initialVersion'], saved=saved,
                            save_selection=saves[-1], selections=[row['value'] for row in selections])
                        observations['map.style'] = styled['actual']
                        self.retain('authoring-style', styled)
                    except (PackError, ValueError, TypeError, KeyError):
                        self.retain('authoring-style-failure', {'code': 'original-model-style-unavailable-or-invalid'})
                self.retain('authoring-final', {'resource': saved, 'nativeReferences': {
                    'version': version_ref, 'current': current_ref, 'draft': draft_ref}})
                for name, declaration in declarations.items():
                    if declaration.get('profile') == 'honua.managed-job-bindings/v1':
                        continue
                    from agent_e2e_native_authoring_bindings import retain
                    reports[name] = retain(self.durable, self.context, name=name, declaration=declaration,
                        responses={'saveHandle': saves[-1]['nativeReferences']['handle'], 'savedVersion': version_ref, 'currentItem': current_ref},
                        selection=saves[-1], started_at=native_context['startedAt'], completed_at=completed)
                rendering = getattr(self, 'config', {}).get('rendering')
                if 'render.actual' in observations and rendering is not None:
                    try:
                        from agent_e2e_studio_render_backend import observe
                        rendered = observe(rendering, studio=self.config['studio'], declarations=declarations,
                            reports=reports, context=native_context, selection=saves[-1], durable=self.durable)
                        observations['render.actual'] = rendered['actual']
                        self.retain('authoring-render', rendered)
                    except (PackError, ValueError, TypeError, KeyError):
                        self.retain('authoring-render-failure', {'code': 'original-browser-render-unavailable-or-invalid'})
        expected = expected_case['assertions']
        failed = []
        for assertion in self.case['assertions']:
            try:
                from agent_e2e_native_bindings import resolve
                wanted = resolve(assertion, expected[assertion], declarations, reports, native_context)
                gate.oracle_predicates.verify(assertion, observations[assertion], wanted)
            except (ValueError, TypeError, PackError): failed.append(assertion)
        result = {'status': 'fail' if failed else 'pass', 'failedAssertions': failed, 'observations': observations,
            'unsupportedAssertions': [assertion for assertion in self.case['assertions'] if assertion not in
                ({'map.composition', 'state.saved', 'source-content.unchanged'} |
                 ({'map.style'} if getattr(self, 'config', {}).get('styling') is not None else set()) |
                 ({'job.completed', 'buffer.metric'} if getattr(self, 'config', {}).get('managedJob') is not None else set()) |
                 ({'render.actual'} if getattr(self, 'config', {}).get('rendering') is not None else set()))],
            'modelSaveCount': len(saves), 'modelCreateCount': sum(row['value']['tool'] == CREATE for row in selections)}
        self.retain('verification', result)
        return {key: result[key] for key in ('status', 'failedAssertions')}


def handle(operation, payload, *, durable, campaign, bundle, configuration, timeout):
    require(operation != 'export_campaign', 'Studio development authoring cannot export complete 252-case campaign evidence')
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
    raise PackError('unsupported native Studio authoring operation')
