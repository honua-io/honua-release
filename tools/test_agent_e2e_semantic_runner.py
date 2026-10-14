"""Observer boundary tests; no fixture response is live semantic evidence."""
import io
import json

import pytest

from agent_e2e_pack import PackError
from agent_e2e_semantic_runner import HttpObserver, NoRedirect, pointer


def test_pointer_reads_response_fields_and_escaped_names():
    assert pointer({'a/b':{'~x':[1, 2]}}, '/a~1b/~0x/1') == 2
    with pytest.raises(PackError):
        pointer({}, 'not-a-pointer')


@pytest.mark.parametrize('endpoint', ['http://remote.example', 'https://user:pass@example.test',
                                      'https://example.test/?token=hidden'])
def test_untrusted_endpoint_rejected_before_credential_lookup(endpoint):
    with pytest.raises(PackError):
        HttpObserver(endpoint, 'NONEXISTENT_OBSERVER_CREDENTIAL')


def test_redirect_cannot_forward_observer_credentials():
    with pytest.raises(PackError, match='redirect'):
        NoRedirect().redirect_request(None, None, 302, '', {}, 'https://another.example')


def test_network_path_reference_and_credential_echo_are_rejected(monkeypatch):
    monkeypatch.setenv('SEMANTIC_TEST_TOKEN', 'private-test-token')
    observer = HttpObserver('https://candidate.example', 'SEMANTIC_TEST_TOKEN')
    with pytest.raises(PackError, match='origin'):
        observer.request({'method':'GET', 'path':'//another.example'})
    class Response(io.BytesIO):
        code = 200
    class Opener:
        def open(self, request, timeout):
            return Response(json.dumps({'echo':'private-test-token'}).encode())
    observer.opener = Opener()
    with pytest.raises(PackError, match='credential'):
        observer.request({'method':'GET', 'path':'/query'})
