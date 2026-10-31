import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from local_http import open_local, require_layer_path, require_loopback


class Redirect(BaseHTTPRequestHandler):
    def log_message(self, *_args): pass
    def do_GET(self):
        self.send_response(302)
        self.send_header('Location', 'http://example.invalid/')
        self.end_headers()


def test_loopback_validation_and_redirect_refusal():
    require_loopback('http://127.0.0.1:1234')
    for url in ('http://example.com:1234', 'http://user:pass@127.0.0.1:1234', 'http://127.0.0.1'):
        with pytest.raises(ValueError): require_loopback(url)
    with pytest.raises(ValueError): require_layer_path('/layer?key=secret')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/', headers={'X-API-Key': 'private'})
        with pytest.raises(urllib.error.HTTPError) as error:
            open_local(request, timeout=1)
        assert error.value.code == 302
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
