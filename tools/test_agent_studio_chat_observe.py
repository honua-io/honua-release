import json

from types import SimpleNamespace

from agent_browser_studio import OriginLock, PlaywrightPage, StudioBrowserServer
from agent_e2e_local_runner import ActionLog


class ChatPage:
    def chat_state(self):
        return {'assistantMessages': 1, 'toolCalls': 0, 'streaming': False,
                'status': 'complete', 'text': 'A question?'}

    def screenshot(self):
        return b''


def test_chat_observation_is_recorded_and_only_available_to_scripted_customer(tmp_path):
    log = ActionLog(tmp_path / 'agent', 1, set(), 5)
    browser = StudioBrowserServer('http://127.0.0.1:8137', log, str(tmp_path))
    browser.lock, browser.page = OriginLock('http://127.0.0.1:8137'), ChatPage()
    assert browser.perform('studio_chat_observe', {})[0] == 'fail'
    scripted_log = ActionLog(tmp_path / 'scripted', 1, set(), 5)
    scripted = StudioBrowserServer('http://127.0.0.1:8137', scripted_log, str(tmp_path), scripted_customer=True)
    scripted.lock, scripted.page = OriginLock('http://127.0.0.1:8137'), ChatPage()
    status, body, _ = scripted.perform('studio_chat_observe', {})
    assert status == 'ok' and json.loads(body)['text'] == 'A question?'
    assert scripted_log.rows[0]['tool'] == 'studio_chat_observe'


def test_assistant_tool_calls_are_logged_before_dispatch_and_bounded(tmp_path):
    log = ActionLog(tmp_path / 'actions', 1, set(), 100)
    page = PlaywrightPage.__new__(PlaywrightPage)
    page.origin, page.aborted, page._identity = OriginLock('http://127.0.0.1:8137'), [], None
    page._assistant_log, page._assistant_tool_calls, page._assistant_bound_hit = log, 0, False

    class Route:
        request = SimpleNamespace(url='http://127.0.0.1:8137/api/agent-workspace/studio/mcp', method='POST',
                                  resource_type='fetch', post_data_json={
                                      'method': 'tools/call', 'params': {'name': 'tool', 'arguments': {}}})
        def __init__(self):
            self.dispatched, self.aborted = False, False

        def fetch(self, **kwargs):
            assert len(log.rows) == page._assistant_tool_calls
            self.dispatched = True
            return SimpleNamespace(status=200, headers={}, body=lambda: b'{"result":{}}')

        def fulfill(self, **kwargs):
            pass

        def abort(self, *_args):
            self.aborted = True

    for _ in range(60):
        route = Route()
        page._gate(route)
        assert route.dispatched and not route.aborted
    refused = Route()
    page._gate(refused)
    assert refused.aborted and not refused.dispatched and page._assistant_bound_hit
    assert len(log.rows) == 60 and all(row['status'] == 'pass' for row in log.rows)


def test_assistant_model_calls_are_bounded(tmp_path):
    page = PlaywrightPage.__new__(PlaywrightPage)
    page.origin, page.aborted, page._identity = OriginLock('http://127.0.0.1:8137'), [], None
    page._assistant_log, page._assistant_model_calls, page._assistant_bound_hit = object(), 0, False

    class Route:
        request = SimpleNamespace(url='http://127.0.0.1:8137/api/agent-workspace/studio/ai', method='POST',
                                  headers={})
        def __init__(self):
            self.continued, self.aborted = False, False

        def continue_(self, **kwargs):
            self.continued = True

        def abort(self, *_args):
            self.aborted = True

    for _ in range(60):
        route = Route()
        page._gate(route)
        assert route.continued and not route.aborted
    refused = Route()
    page._gate(refused)
    assert refused.aborted and not refused.continued and page._assistant_bound_hit
