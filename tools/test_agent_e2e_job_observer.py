"""Real independent HTTP transport against controlled native response originals."""
import base64
import copy
from datetime import datetime, timedelta, timezone
import json
import math
from types import SimpleNamespace

import pytest

from agent_e2e_gp_fingerprint import fingerprint
from agent_e2e_job_observer import JobObserver, DATA_PREFIX, PROCESS, metric_buffer
from agent_e2e_pack import PackError, canonical_digest
from test_agent_e2e_scenario_backend import adapter
from test_agent_e2e_setup_http import server


@pytest.fixture
def native_job(adapter, server):
    created = datetime.now(timezone.utc)-timedelta(seconds=2)
    binding = {'requesterPrincipalId':'api-key:task-owner', 'notBefore':(created-timedelta(seconds=1)).isoformat()}
    plan = {'planId':'one-buffer', 'intentId':'shelter', 'steps':[{'stepId':'buffer','kind':'Geoprocess',
        'processId':PROCESS,'inputs':{'distance':'500','dissolve':'false'}}], 'outputs':['FeatureLayer']}
    action = {'name':'mcp_call_tool','input':{'name':'honua_execute_plan','arguments':{'plan':plan}}}
    output = {'jobId':'gp-actual-job','status':'Queued','createdAt':created.isoformat(),'resourceUri':'honua://jobs/gp-actual-job'}
    worker = {'agentResult':{'isError':False,'errorCategory':None,'content':{'isError':False,'structuredContent':output}}}
    dispatch = {'actionId':'a'*64,'ordinal':1,'requestDigest':canonical_digest(action)}
    detail = {'jobId':output['jobId'],'kind':'Geoprocessing','requestedBy':binding['requesterPrincipalId'],
        'requestFingerprint':fingerprint(plan),'status':'Running','createdAt':created.isoformat(),
        'updatedAt':(created+timedelta(seconds=1)).isoformat(),'completedAt':None}
    ring = [[500*math.cos(index*math.pi/16),500*math.sin(index*math.pi/16)] for index in range(32)]
    ring.append(ring[0])
    document = {'type':'FeatureCollection','processId':PROCESS,'featureCount':1,
        'honua:spatialReference':{'schemaVersion':'honua.feature-crs/v1','state':'known','srid':3857},
        'features':[{'type':'Feature','properties':{'source_id':'shelter-first','COUNT':1},
                     'geometry':{'type':'Polygon','coordinates':[ring]}}]}
    state = SimpleNamespace(detail=detail,document=document,records={},status=200,href=None)
    def retain(name, value):
        assert name not in state.records
        state.records[name] = copy.deepcopy(value)
    def respond(method,path,raw):
        assert method == 'GET', 'observer must never submit/replay a model mutation'
        if path == '/api/v1/admin/jobs/gp-actual-job':
            value = {'success':True,'data':state.detail}
        elif path == '/ogc/processes/jobs/gp-actual-job/results':
            value = {'outputs':{'buffer':{'id':'native-artifact','kind':'FeatureLayer','type':'application/geo+json',
                'href':state.href or DATA_PREFIX+base64.b64encode(json.dumps(state.document).encode()).decode()}}}
        elif path == '/api/geoprocessing/jobs/gp-actual-job/artifacts/0/content':
            value = state.document
        else:
            raise AssertionError('foreign/unreviewed resource read: '+path)
        return state.status, {'Content-Type':'application/json'}, json.dumps(value).encode()
    server.respond = respond
    observer = JobObserver(http=adapter.http,binding=binding,retain=retain)
    def select(): return observer.selected(action,dispatch,worker)
    def succeed():
        state.detail.update(status='Succeeded',completedAt=(created+timedelta(seconds=1)).isoformat())
    return SimpleNamespace(observer=observer,state=state,select=select,succeed=succeed,action=action,
        worker=worker,dispatch=dispatch,binding=binding)


@pytest.mark.parametrize('staged',[False,True])
def test_original_plan_joins_independent_native_job_and_producer_crs_artifact(native_job, server, staged):
    value = native_job
    selected = value.select()
    assert selected['initial']['resource']['status'] == 'Running'
    assert selected['requestFingerprint'] == value.state.detail['requestFingerprint']
    value.succeed()
    if staged:
        value.state.href = server.url+'/api/geoprocessing/jobs/gp-actual-job/artifacts/0/content'
    result = value.observer.results(selected,step='final')
    assert result['resource']['status'] == 'Succeeded'
    assert result['buffer']['srid'] == 3857 and result['buffer']['sourceId'] == 'shelter-first'
    assert result['document'] == value.state.document
    assert set(result['nativeReferences']) == ({'completed','results','content'} if staged else {'completed','results'})
    assert all(method == 'GET' for method,*_ in server.seen)


@pytest.mark.parametrize('field,value', [('requestedBy','another-owner'),('requestFingerprint','a'*64),
    ('requestFingerprint',None),('jobId','another-job'),('kind','Import'),('createdAt','2000-01-01T00:00:00Z'),
    ('updatedAt','2999-01-01T00:00:00Z')])
def test_uncorroborated_job_cannot_become_model_execution_evidence(native_job,field,value):
    native_job.state.detail[field] = value
    with pytest.raises(PackError): native_job.select()
    assert not native_job.state.records


def test_model_success_claim_does_not_replace_authorized_independent_read(native_job):
    native_job.state.status = 403
    with pytest.raises(PackError): native_job.select()
    assert not native_job.state.records


def test_changed_original_plan_or_synchronous_tool_cannot_join_accepted_job(native_job):
    native_job.action['input']['arguments']['plan']['steps'][0]['inputs']['distance'] = '501'
    native_job.dispatch['requestDigest'] = canonical_digest(native_job.action)
    with pytest.raises(PackError): native_job.select()
    native_job.action['input']['name'] = 'honua_query_features'
    with pytest.raises(PackError): native_job.select()


@pytest.mark.parametrize('state',['Running','Failed','Cancelled'])
def test_non_success_does_not_read_or_qualify_outputs(native_job,server,state):
    selected = native_job.select()
    native_job.succeed()
    native_job.state.detail['status'] = state
    with pytest.raises(PackError): native_job.observer.results(selected,step='final')
    assert not any(path.endswith('/results') for _,path,*_ in server.seen)


@pytest.mark.parametrize('href',['https://foreign.example/api/geoprocessing/jobs/gp-actual-job/artifacts/0/content',
    '/api/geoprocessing/jobs/other/artifacts/0/content','/api/v1/admin/keys','data:application/json;base64,e30=',
    '/api/geoprocessing/jobs/gp-actual-job/artifacts/0/content?token=unexpected'])
def test_artifact_read_cannot_escape_same_candidate_and_job(native_job,href):
    selected = native_job.select(); native_job.succeed(); native_job.state.href = href
    with pytest.raises(PackError): native_job.observer.results(selected,step='final')


@pytest.mark.parametrize('crs',[None,{'schemaVersion':'honua.feature-crs/v1','state':'unknown','srid':3857},
    {'schemaVersion':'honua.feature-crs/v1','state':'known','srid':4326},
    {'schemaVersion':'honua.feature-crs/v1','state':'known','srid':'3857'}])
def test_expected_metric_frame_cannot_fill_missing_or_false_artifact_crs(native_job,crs):
    native_job.state.document['honua:spatialReference'] = crs
    with pytest.raises(PackError): metric_buffer(native_job.state.document)


def test_projection_preserves_wrong_geometry_for_unchanged_metric_oracle(native_job):
    import agent_e2e_gate as gate
    native_job.state.document['features'][0]['geometry']['coordinates'][0][1][0] += 100
    observed = metric_buffer(native_job.state.document)
    with pytest.raises(ValueError):
        gate.oracle_predicates.verify('buffer.metric',observed,{'sourceId':'shelter-first','center3857':[0,0],
            'radiusMetres':500,'radialToleranceMetres':0.01,'expectedArea':math.pi*500*500,'areaRelativeTolerance':0.01})
