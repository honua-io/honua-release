"""Bounded same-issued-key OGC item probes; not a P16 backend or qualification.

The caller must separately prove model issuance/revocation, native operation
approval and original worker custody. This component never issues or revokes keys.
"""
from __future__ import annotations

import copy
import re

from agent_e2e_pack import PackError, canonical_digest


def require(value, reason):
    if not value:
        raise PackError(reason)


def feature(answer, feature_id):
    observation = answer['observation']
    value = observation['body']
    require(observation['httpStatus'] == 200 and isinstance(value, dict) and
            value.get('type') == 'Feature' and str(value.get('id')) == feature_id and
            isinstance(value.get('properties'), dict) and 'geometry' in value and
            'error' not in value and value.get('success', True) is not False,
            'key probe lacks exact authorized owned feature')
    return {field: copy.deepcopy(value[field]) for field in ('type', 'id', 'geometry', 'properties')}


class KeyPermissionProbe:
    def __init__(self, *, issued, independent, key_http, resource):
        require(isinstance(resource, dict) and set(resource) == {'collectionId', 'featureId', 'replacement'} and
                isinstance(resource['collectionId'], str) and
                re.fullmatch(r'[A-Za-z0-9_-]{1,100}', resource['collectionId']) and
                isinstance(resource['featureId'], str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', resource['featureId']),
                'key probe requires fixed bounded OGC item identity')
        replacement = resource['replacement']
        require(isinstance(replacement, dict) and set(replacement) == {'type', 'id', 'geometry', 'properties'} and
                replacement['type'] == 'Feature' and str(replacement['id']) == resource['featureId'] and
                isinstance(replacement['properties'], dict), 'key probe requires fixed valid replacement feature')
        require(independent.durable is key_http.durable and independent.context == key_http.context and
                independent.client.endpoint == key_http.client.endpoint and
                independent.client.token != key_http.client.token and
                independent.identity['attemptId'] != key_http.identity['attemptId'],
                'key probe requires distinct independent same-attempt observer')
        issued.assert_independent(independent)
        issued.screen(key_http)
        self.issued, self.independent, self.key_http = issued, independent, key_http
        self.resource = copy.deepcopy(resource)
        self.path = '/ogc/features/collections/'+resource['collectionId']+'/items/'+resource['featureId']
        self.before = None
        # The consume identity itself remains guarded separately by OperationKeyConsumer.
        scope = {field: key_http.context[field] for field in ('campaignId', 'caseId', 'attempt', 'componentBindingDigest')}
        self.probe_id = canonical_digest({'scope': scope, 'resource': resource})

    def start(self):
        http = self.independent
        require(not any(value.get('producer') == 'p16-item-key-probe' and value.get('probeId') == self.probe_id
                        for value in http.durable.state.get('scenarioOperations', {}).values()),
                'key probe already started; reconcile original evidence without replay')
        http.authorize()
        http.retain('intent', 'p16-item-probe', {'producer': 'p16-item-key-probe', 'probeId': self.probe_id,
                    'resourceDigest': canonical_digest(self.resource)})
        before = http.json('p16-item-before', 'GET', self.path)
        snapshot = feature(before, self.resource['featureId'])
        require(snapshot != self.resource['replacement'], 'key probe replacement must actually change owned data')
        read = self.issued.probe(self.key_http, 'p16-issued-read', 'GET', self.path)
        require(feature(read, self.resource['featureId']) == snapshot, 'issued key read differs from owned item')
        denial = self.issued.probe(self.key_http, 'p16-issued-write', 'PUT', self.path, self.resource['replacement'])
        require(denial['observation']['httpStatus'] == 403, 'issued key did not receive write authorization denial')
        after = http.json('p16-item-after-write', 'GET', self.path)
        require(feature(after, self.resource['featureId']) == snapshot, 'denied write changed owned item data')
        self.before = snapshot
        return {'origin': 'controller-issued-key-probe', 'qualification': False,
                'read': read, 'writeDenial': denial, 'before': before, 'after': after}

    def after_revocation(self):
        require(self.before is not None, 'key probe lacks original positive control')
        # No controller revoke here. Invocation does not itself establish that the
        # model selected a revoke or that native approval completed.
        denied = self.issued.probe(self.key_http, 'p16-issued-after-revoke', 'GET', self.path)
        require(denied['observation']['httpStatus'] == 401, 'same issued key still authenticates or only lacks scope')
        after = self.independent.json('p16-item-after-revoke', 'GET', self.path)
        require(feature(after, self.resource['featureId']) == self.before, 'owned item changed across credential lifecycle')
        return {'origin': 'controller-issued-key-probe', 'qualification': False,
                'authenticationDenial': denied, 'after': after}
