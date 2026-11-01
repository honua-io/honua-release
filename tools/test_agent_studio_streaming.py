"""Browser proof for the Console assistant's streaming transport."""
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_browser_studio import OriginLock, PlaywrightPage


def test_console_assistant_stream_does_not_block_browser_actions():
    pytest.importorskip('playwright.sync_api')
    started, release = threading.Event(), threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == '/':
                body = (b'<button id="go" onclick="fetch(\'/api/agent-workspace/agent/ai\', '
                        b'{method:\'POST\'}).then(r => r.body.getReader().read()).then(() => '
                        b'document.querySelector(\'#status\').textContent=\'received\')">Start</button>'
                        b'<span id="status">waiting</span>')
                self.send_response(200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def do_POST(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(b'data: first\n\n')
            self.wfile.flush()
            started.set()
            release.wait(8)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    page = None
    try:
        page = PlaywrightPage(OriginLock(f'http://127.0.0.1:{server.server_port}'))
        page.goto('/')
        timer = threading.Timer(2, release.set)
        timer.start()
        page.click('#go')
        page._page.wait_for_timeout(100)
        assert started.is_set()
        page._page.get_by_text('received').wait_for(timeout=1500)
        assert 'received' in page.snapshot_text()
        assert not release.is_set(), 'assistant response was buffered until the stream closed'
    finally:
        release.set()
        if page is not None:
            page.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_console_assistant_stream_cannot_follow_cross_origin_redirect():
    pytest.importorskip('playwright.sync_api')
    reached = threading.Event()

    class Foreign(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            reached.set()
            self.send_response(204)
            self.end_headers()

    foreign = ThreadingHTTPServer(('127.0.0.1', 0), Foreign)

    class Console(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = (b'<button id="go" onclick="fetch(\'/api/agent-workspace/agent/ai\', '
                    b'{method:\'POST\'}).catch(() => document.querySelector(\'#status\').textContent=\'blocked\')">'
                    b'Start</button><span id="status">waiting</span>')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.send_response(307)
            self.send_header('Location', f'http://127.0.0.1:{foreign.server_port}/leak')
            self.end_headers()

    console = ThreadingHTTPServer(('127.0.0.1', 0), Console)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (console, foreign)]
    for thread in threads:
        thread.start()
    page = None
    try:
        identity = {'edgeSecret': 'test-edge-secret', 'subject': 'test-user', 'mintToken': lambda: 'Bearer test-token'}
        page = PlaywrightPage(OriginLock(f'http://127.0.0.1:{console.server_port}'), identity=identity)
        page.goto('/')
        page.click('#go')
        page._page.get_by_text('blocked').wait_for(timeout=3000)
        assert not reached.is_set()
    finally:
        if page is not None:
            page.close()
        for server in (console, foreign):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)
