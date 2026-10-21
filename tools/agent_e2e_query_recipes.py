"""Reviewed HTTP recipes for the first three independent semantic checks.

These deterministic probes are not agent scenario solutions. Layer IDs must be
bound from controller-observed publication, never guessed or supplied by a model.
The complete semantic runner still requires every case in CASES.
"""
from urllib.parse import quote, urlencode

from agent_e2e_pack import PackError


def point_selection_recipe(service, layer_id, case):
    clauses = {'select-strict': 'capacity > 50', 'select-inclusive': 'capacity >= 50',
               'select-inclusive-null': 'capacity >= 50 OR capacity IS NULL'}
    if (case not in clauses or not isinstance(service, str) or not service.strip()
            or len(service) > 200 or service in ('.', '..') or any(char in service for char in '/\\')
            or any(ord(char) < 32 for char in service)
            or type(layer_id) is not int or not 0 <= layer_id <= 2147483647):
        raise PackError('invalid observed point-layer recipe binding')
    path = f'/rest/services/{quote(service, safe="")}/FeatureServer/{layer_id}/query'
    def query(where):
        return {'method': 'GET', 'path': path+'?'+urlencode({
            'f': 'json', 'where': where, 'outFields': '*', 'returnGeometry': 'true',
            'outSR': 3857, 'orderByFields': 'source_id ASC', 'resultRecordCount': 1000})}
    def field(name):
        return {'response': 0, 'pointer': '/body', 'normalizer': 'feature-server-points/v1',
                'resultPointer': '/'+name}
    return {'source': {'requests': [query('1=1')],
                       'result': {'srid': field('srid'), 'features': field('features')}},
            'requests': [query(clauses[case])],
            'result': {'srid': field('srid'), 'features': field('features')}}
