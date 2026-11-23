"""Which failed API answers the resolver waits out (retry) and which refuse at once."""
import email.message
import subprocess
import urllib.error

import pytest

import resolve_trunk_candidate as resolver


@pytest.mark.parametrize('stderr', [
    'gh: Server Error (HTTP 500)', 'gh: Server Error (HTTP 502)',
    'gh: Service Unavailable (HTTP 503)', 'gh: Gateway Timeout (HTTP 504)',
    'gh: Too Many Requests (HTTP 429)'])
def test_a_gh_cli_server_error_is_retried(monkeypatch, stderr):
    calls, sleeps = [], []
    def request():
        calls.append('same')
        if len(calls) < 2:
            raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr=stderr)
        return 42
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    assert resolver.retry(request) == 42
    assert sleeps == [10]


@pytest.mark.parametrize('stderr', [
    'gh: Resource not accessible by integration (HTTP 403)',
    'gh: Must have admin rights to Repository. (HTTP 403)',
    # A 403 anywhere in the text is not a 403 answer.
    'gh: Not Found (HTTP 404) for sha 403a'])
def test_a_permanent_gh_refusal_is_not_retried(monkeypatch, stderr):
    calls = []
    def request():
        calls.append('same')
        raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr=stderr)
    monkeypatch.setattr(resolver.time, 'sleep', lambda _: pytest.fail('a permanent refusal is not transient'))
    with pytest.raises(subprocess.CalledProcessError):
        resolver.retry(request)
    assert calls == ['same']


@pytest.mark.parametrize('stderr', [
    'gh: API rate limit exceeded for installation ID 1. (HTTP 403)',
    'gh: You have exceeded a secondary rate limit. Please wait a few minutes before you try again. (HTTP 403)'])
def test_a_gh_rate_limit_403_is_retried(monkeypatch, stderr):
    calls, sleeps = [], []
    def request():
        calls.append('same')
        if len(calls) < 2:
            raise subprocess.CalledProcessError(1, ['gh', 'api'], stderr=stderr)
        return 42
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    assert resolver.retry(request) == 42
    assert sleeps == [10]


def http_error(code, headers=None):
    return urllib.error.HTTPError('https://ghcr.io/v2/x', code, 'refused', email.message.Message() if headers is None
                                  else headers, None)


def rate_headers(**values):
    headers = email.message.Message()
    for name, value in values.items():
        headers[name.replace('_', '-')] = value
    return headers


@pytest.mark.parametrize('error,retried', [
    (lambda: http_error(403), False),
    (lambda: http_error(401), False),
    (lambda: http_error(403, rate_headers(Retry_After='5')), True),
    (lambda: http_error(403, rate_headers(X_RateLimit_Remaining='0')), True),
    (lambda: http_error(429), True),
    (lambda: http_error(503), True)])
def test_an_http_403_is_retried_only_as_a_rate_limit(monkeypatch, error, retried):
    calls, sleeps = [], []
    def request():
        calls.append('same')
        if len(calls) < 2:
            raise error()
        return 42
    monkeypatch.setattr(resolver.time, 'sleep', sleeps.append)
    if retried:
        assert resolver.retry(request) == 42 and sleeps == [10]
    else:
        with pytest.raises(urllib.error.HTTPError):
            resolver.retry(request)
        assert calls == ['same'] and sleeps == []
