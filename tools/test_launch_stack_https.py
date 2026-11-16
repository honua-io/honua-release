"""Fault tests for effective frontend HSTS observations; no network access."""
import copy
import json

import pytest

from launch_stack_https import HttpsObservationError, observe_https


POLICY = 'max-age=31536000; includeSubDomains; preload'
ORIGIN = 'https://qualification.example.test'


def transport(headers=None, **overrides):
    calls = []

    def request(url, *, follow_redirects):
        calls.append((url, follow_redirects))
        response = {'url': url, 'status': 200, 'redirected': False,
                    'headers': [('Strict-Transport-Security', POLICY), ('Set-Cookie', 'private')]}
        if headers is not None:
            response['headers'] = copy.deepcopy(headers)
        response.update(overrides)
        return response

    return request, calls


def test_both_effective_routes_are_observed_without_disclosing_headers():
    request, calls = transport()
    result = observe_https(request, ORIGIN)
    assert calls == [(ORIGIN + '/console/', False), (ORIGIN + '/healthz/ready', False)]
    assert result['responses'] == [
        {'path': '/console/', 'status': 200, 'hstsMaxAge': 31536000},
        {'path': '/healthz/ready', 'status': 200, 'hstsMaxAge': 31536000}]
    encoded = json.dumps(result)
    assert 'private' not in encoded and ORIGIN not in encoded
    assert set(result) == {'startedAt', 'completedAt', 'responses'}


@pytest.mark.parametrize('origin', [
    'http://qualification.example.test', 'https://user:secret@example.test',
    'https://@example.test', 'https://example.test\n',
    'https://example.test:8443', 'https://example.test/console/',
    'https://example.test?value=secret', 'https://example.test#secret', 'https:///', 'https://example.test/?', 'https://example.test/#'])
def test_invalid_origin_fails_before_transport(origin):
    request, calls = transport()
    with pytest.raises(ValueError):
        observe_https(request, origin)
    assert not calls


@pytest.mark.parametrize('policy', [
    '', 'max-age=\u00a031536000; includeSubDomains; preload',
    'max-age=\v31536000; includeSubDomains; preload',
    'max-age=\f31536000; includeSubDomains; preload', 'max-age=31536000', 'max-age=31536000; preload',
    'max-age=31536000; includeSubDomains', 'max-age=0; includeSubDomains; preload',
    'max-age=31535999; includeSubDomains; preload',
    'max-age=31536000; max-age=0; includeSubDomains; preload',
    'max-age=-1; includeSubDomains; preload', 'max-age=1e9; includeSubDomains; preload',
    'max-age=31536000; includeSubDomains=1; preload',
    'max-age=31536000; includeSubDomains; preload=1',
    'max-age=31536000; includeSubDomains; preload, max-age=0',
    'max-age=31536000; includeSubDomains; preload\r\nSet-Cookie: private',
    'max-age=999999999999999999; includeSubDomains; preload'])
def test_inadequate_or_ambiguous_policy_fails(policy):
    request, _ = transport([('Strict-Transport-Security', policy)])
    with pytest.raises(HttpsObservationError):
        observe_https(request, ORIGIN)


@pytest.mark.parametrize('headers', [
    [], {}, [('Strict-Transport-Security', POLICY)] * 2,
    [('strict-transport-security', POLICY), ('Strict-Transport-Security', 'max-age=0')],
    [('Strict-Transport-Security', None)], [('Strict-Transport-Security',)],
    [('Strict-Transport-Security\r\n', POLICY)]])
def test_duplicate_missing_or_malformed_headers_fail(headers):
    request, _ = transport(headers)
    with pytest.raises(HttpsObservationError):
        observe_https(request, ORIGIN)


@pytest.mark.parametrize('overrides', [
    {'url': 'https://other.example.test/console/'}, {'redirected': True},
    {'redirected': None}, {'status': 302}, {'status': 503}, {'status': '200'}])
def test_redirects_and_unsuccessful_responses_fail(overrides):
    request, _ = transport(**overrides)
    with pytest.raises(HttpsObservationError):
        observe_https(request, ORIGIN)


def test_api_failure_cannot_be_hidden_by_successful_console():
    request, _ = transport()

    def missing_api_hsts(url, **options):
        response = request(url, **options)
        if url.endswith('/healthz/ready'):
            response['headers'] = []
        return response

    with pytest.raises(HttpsObservationError):
        observe_https(missing_api_hsts, ORIGIN)


def test_certificate_or_network_failure_propagates():
    def unavailable(*args, **kwargs):
        raise OSError('TLS certificate verification failed')

    with pytest.raises(OSError):
        observe_https(unavailable, ORIGIN)


def test_case_whitespace_and_quoted_decimal_policy():
    request, _ = transport([('strict-transport-security',
                            ' PRELOAD ; MAX-AGE = "63072000" ; includeSubDomains ')])
    result = observe_https(request, ORIGIN + '/')
    assert all(row['hstsMaxAge'] == 63072000 for row in result['responses'])
