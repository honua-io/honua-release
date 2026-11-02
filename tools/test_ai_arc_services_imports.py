"""Rehearsal of record r2 (2026-11-02): the services handoff states where the customer's imported tables are."""
from types import SimpleNamespace

import pytest

import ai_arc_services

IMPORTS = {'connectionId': '6f1c2a10-1111-4222-8333-944455556666', 'connectionName': 'arc-abc-p10-fixture',
           'datasets': [{'dataset': 'flood_zones', 'schema': 'honua_data', 'table': 'arc10_abc_flood_zones'},
                        {'dataset': 'parcels', 'schema': 'honua_data', 'table': 'arc10_abc_parcels'},
                        {'dataset': 'shelters', 'schema': 'honua_data', 'table': 'arc10_abc_shelters'}]}
GOAL = ('Our Kestrel shelter list was imported into Honua but nobody can see it yet. Please publish it.\n\n'
        'When you are finished, end your reply with a short checklist.')


def test_the_note_names_the_connection_and_every_imported_dataset_and_no_tool():
    note = ai_arc_services.customer_import_note(IMPORTS)
    assert IMPORTS['connectionId'] in note and IMPORTS['connectionName'] in note
    for row in IMPORTS['datasets']:
        assert f"table {row['table']} in schema {row['schema']}" in note
    assert 'flood zones' in note
    lowered = note.lower()
    import re
    assert not re.search(r'honua_(?!data\b)[a-z]', lowered)          # no tool name (the schema is honua_data)
    assert 'publish' not in lowered and 'tool' not in lowered
    assert '50' not in note and 'kestrel' not in lowered        # no part of the request or its answer


@pytest.mark.parametrize('bad', [{}, {'connectionId': 'x', 'datasets': []},
                                 {'connectionId': '', 'datasets': IMPORTS['datasets']},
                                 {'connectionId': 'x', 'datasets': [{'dataset': 'shelters', 'schema': 'honua_data'}]}])
def test_an_incomplete_import_record_is_refused(bad):
    with pytest.raises(ValueError):
        ai_arc_services.customer_import_note(bad)


def _runtime(tmp_path):
    return ai_arc_services.runtime_class()(
        base_url='http://127.0.0.1:1', admin_key='unused', project='arc-test',
        candidate_digest='sha256:' + 'a' * 64, agent='codex-cli', model='test',
        limits={'maxTurns': 1, 'maxToolCalls': 1, 'maxSeconds': 1}, private_root=tmp_path)


def _records(root, layers):
    import base64, json
    root.mkdir(parents=True)
    for name, table in layers.items():
        body = {'success': True, 'physicalTableName': table, 'schema': 'honua_data'}
        (root / f"import-{name.replace('_', '-')}.json").write_text(json.dumps(
            {'step': 'import-' + name, 'status': 200, 'bodyBase64': base64.b64encode(json.dumps(body).encode()).decode()}))
    return root


TABLES = {'parcels': 'arc10_abc_parcels', 'flood_zones': 'arc10_abc_flood_zones', 'shelters': 'arc10_abc_shelters'}


def _stack(tmp_path, project='arc-abc-p10', layers=TABLES):
    root = _records(tmp_path / project / 'controller-records', layers) if layers else tmp_path / project / 'none'
    return SimpleNamespace(compose_project=project, extra={
        'recorder': SimpleNamespace(root=root), 'connectionId': IMPORTS['connectionId'],
        'connectionName': IMPORTS['connectionName']})


def test_the_import_record_comes_from_the_harness_own_import_responses(tmp_path):
    imports = ai_arc_services.customer_imports(_stack(tmp_path))
    assert imports == {'connectionId': IMPORTS['connectionId'], 'connectionName': IMPORTS['connectionName'],
                       'datasets': [{'dataset': n, 'schema': 'honua_data', 'table': TABLES[n]}
                                    for n in ('parcels', 'flood_zones', 'shelters')]}
    assert ai_arc_services.customer_imports(_stack(tmp_path, 'arc-abc-p15', layers=None)) is None
    with pytest.raises(Exception):      # a partial import record is never handed off
        ai_arc_services.customer_imports(_stack(tmp_path, 'arc-abc-px', layers={'shelters': 'arc10_abc_shelters'}))


def test_the_first_session_gets_the_note_and_the_goal_text_is_untouched(tmp_path, monkeypatch):
    import agent_e2e_local_runner as runner
    sent = []
    monkeypatch.setattr(runner.LocalRuntime, 'session',
                        lambda self, stack, prompt, number, **kw: sent.append((number, prompt)) or {'finalAnswer': 'ok'})
    runtime = _runtime(tmp_path)
    stack = _stack(tmp_path)
    runtime.session(stack, GOAL, 1)
    runtime.session(stack, 'the first answer', 2)
    first, second = sent
    note = ai_arc_services.customer_import_note(ai_arc_services.customer_imports(stack))
    assert first[1] == note + '\n\n' + GOAL
    assert first[1].endswith(GOAL)                       # the case's own request, verbatim, last
    for table in TABLES.values():
        assert table in first[1]
    assert second == (2, 'the first answer')             # follow-ups are never changed
    runtime.session(_stack(tmp_path, 'arc-abc-p15', layers=None), GOAL, 1)
    assert sent[-1] == (1, GOAL)                         # no import on this case: nothing is added


def test_the_scorer_digest_is_unchanged_by_the_handoff():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    import ai_arc_runner as R
    # The handoff lives in the runner (ai_arc_services); no scorer-bound file (SCORER_GLOBS) carries it.
    for pattern in R.SCORER_GLOBS:
        for path in (sorted(root.glob(pattern)) if '*' in pattern else [root / pattern]):
            if path.is_file() and path.suffix == '.py':
                assert 'customer_import' not in path.read_text(encoding='utf-8'), path
