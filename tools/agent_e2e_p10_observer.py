"""Independent replay of the model's actual P10 query; never supplies a filter."""
import copy
from urllib.parse import quote, urlencode

from agent_e2e_mcp import Client
from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_query_projection import feature_server_points


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def source_path(binding):
    return '/rest/services/'+quote(binding['service'], safe='')+'/FeatureServer/'+str(binding['layerId'])+'/query?'+urlencode({
        'f': 'json', 'where': '1=1', 'outFields': '*', 'returnGeometry': 'true',
        'outSR': 3857, 'orderByFields': 'source_id ASC', 'resultRecordCount': 1000})


def source_content(answer):
    require(answer['observation']['httpStatus'] == 200, 'P10 independent source read failed')
    value = feature_server_points(answer['observation']['body'])
    value['features'].sort(key=lambda feature: feature['properties']['source_id'])
    return {'resourceId': 'shelters', 'content': value}


def query_ids(result, arguments, binding):
    require(isinstance(result, dict) and result.get('isError', False) is False,
            'P10 selected query failed')
    value = result.get('structuredContent')
    require(isinstance(value, dict) and value.get('serviceId') == binding['service'] and
            value.get('layerId') == binding['layerId'] and value.get('exceededTransferLimit') is False and
            value.get('resultOffset') == 0 and arguments.get('returnCountOnly', False) is False,
            'P10 query is foreign, count-only or incomplete')
    collection = value.get('geojson')
    require(isinstance(collection, dict) and collection.get('type') == 'FeatureCollection' and
            isinstance(collection.get('features'), list), 'P10 selected query lacks feature evidence')
    features = collection['features']
    require(type(value.get('returnedCount')) is int and value['returnedCount'] == len(features),
            'P10 query count differs from actual features')
    ids = [feature.get('properties', {}).get('source_id') for feature in features]
    require(all(isinstance(identifier, str) and identifier for identifier in ids) and len(set(ids)) == len(ids),
            'P10 selected query lacks complete distinct source IDs')
    return {'resourceIds': sorted(ids)}


class P10Observer:
    def __init__(self, *, http, mcp, retain, binding, client_factory=Client):
        self.http, self.mcp, self.retain = http, copy.deepcopy(mcp), retain
        self.binding, self.client_factory = copy.deepcopy(binding), client_factory

    def source(self, step):
        answer = self.http.json(step, 'GET', source_path(self.binding))
        return {'snapshot': source_content(answer), 'rawReference': answer['reference']}

    def eligible(self, action):
        if action['name'] != 'mcp_call_tool' or action['input'].get('name') != 'honua_query_features':
            return False
        arguments = action['input']['arguments']
        return (arguments.get('serviceId') == self.binding['service'] and
                arguments.get('layerId') == self.binding['layerId'])

    def replay(self, action, dispatch, before, worker_result):
        """Only an existing model-selected read may reach the privileged observer."""
        require(self.eligible(action), 'P10 replay is not the model-selected owned-layer query')
        arguments = copy.deepcopy(action['input']['arguments'])
        # The source contract is a read-only operation. Preserve every selected
        # argument, including a wrong filter/offset/field choice, for real failure.
        intent = {'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'],
                  'tool': 'honua_query_features', 'arguments': arguments}
        self.retain('query-observer-intent-'+dispatch['actionId'], intent)
        self.http.authorize()
        client = self.client_factory(self.mcp['endpoint'], credential_kind=self.mcp['credentialKind'],
            credential_env=self.mcp['credentialEnv'], workflow_view=self.mcp.get('workflowView'), timeout=120)
        try:
            client.initialize()
            result = client.call_tool('honua_query_features', arguments)
        finally:
            try:
                client.close()
            finally:
                raw_reference = self.retain('query-observer-raw-'+dispatch['actionId'], client.observations)
        after = self.source('after-'+dispatch['actionId'])
        projected, error = None, None
        try:
            projected = query_ids(result, arguments, self.binding)
            worker_ids = query_ids(worker_result['agentResult']['content'], arguments, self.binding)
            require(projected == worker_ids, 'independent P10 replay differs from actual worker result')
        except (PackError, KeyError, TypeError) as exc:
            error = type(exc).__name__
        observation = {'actionId': dispatch['actionId'], 'requestDigest': dispatch['requestDigest'],
            'before': before, 'after': after, 'result': projected, 'projectionError': error,
            'rawReference': raw_reference, 'workerResultDigest': canonical_digest(worker_result['agentResult'])}
        self.retain('query-observation-'+dispatch['actionId'], observation)
        return observation
