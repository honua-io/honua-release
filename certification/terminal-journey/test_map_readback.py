"""Actual HTTP draft/version reads; candidate writes are explicit test receipts."""
import copy
import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
from threading import Thread
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import executor
import stages
from transport import ActionRejected, ExecutionError, Transport

BODY = {"format": "honua_map_package.v1", "mapPackageId": "journey-map",
        "sourceBindings": [{"sourceId": "journey", "locator": {"collectionId": "7"}}],
        "mapSpec": {"version": 8, "sources": {"journey": {"data": "/ogc/features/collections/7/items"}},
                    "layers": [{"id": "journey-layer", "type": "circle", "paint": {"circle-radius": 8}}]},
        "initialView": {"center": [0, 0], "zoom": 4}}
DRAFT = {"draftId": "draft", "itemId": "item", "generation": 3, "ownerId": "owner",
         "workspaceId": "actual-native-workspace", "packageKey": "journey-map", "family": "map",
         "envelope": {"family": "map", "body": BODY}}
VERSION = {"itemId": "item", "versionId": "version", "contentHash": "native-hash",
           "ownerId": "owner", "packageKey": "journey-map", "sourceDraftId": "draft",
           "envelope": {"family": "map", "body": BODY}}
SAVE = {"operation": {"operationInstanceId": "save-operation", "correlationId": "save-correlation",
                       "auditId": "save-audit"}, "version": VERSION}
DRAFT_PATH = "/api/v1/studio/package-drafts/draft"
VERSION_PATH = "/api/v1/studio/content-items/item/versions/version"


@pytest.fixture
def http_map():
    servers = []
    def start():
        documents, seen = {}, []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("X-API-Key")))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(documents[self.path])
            def log_message(self, *_):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=lambda: server.serve_forever(poll_interval=0.02))
        thread.start()
        servers.append((server, thread))
        return f"http://127.0.0.1:{server.server_port}", documents, seen
    try:
        yield start
    finally:
        for server, thread in servers:
            server.shutdown()
            thread.join()
            server.server_close()


def document(value):
    return json.dumps({"success": True, "data": value}, indent=2).encode()


def setup_map(start, *, body=None):
    primary, originals, seen = start()
    replica, replica_originals, replica_seen = start()
    draft = copy.deepcopy(DRAFT)
    if body is not None:
        draft["envelope"]["body"] = copy.deepcopy(body)
    originals['/api/v1/studio/package-families'] = document({'families': [
        {'family': 'map', 'currentSchemaVersion': '1.0'}]})
    originals[DRAFT_PATH] = document(draft)
    originals[VERSION_PATH] = document(VERSION)
    replica_originals[VERSION_PATH] = document(VERSION)
    transport = Transport(primary, None, None, Path('/tmp'), {"proposer": "private-key"})
    transport.tool = mock.Mock(return_value={"structuredContent": copy.deepcopy(SAVE)})
    observation = stages.Observation(setup_view_present=True, setup_discovery={"metadata": {}, "tools": [
        {"name": name, "inputSchema": {"type": "object"}}
        for name in ("honua_studio_save_version", "honua_studio_update_draft", "honua_studio_create_draft")]})
    engine = executor.JourneyExecutor({"workspaceId": "workspace"}, {
        "replicaBaseUrl": replica, "execution": {"mapBody": copy.deepcopy(BODY)}}, observation, transport)
    engine.resources.update(draftId="draft", itemId="item", generation=3)
    engine.record_map_draft("honua_studio_create_draft", {"body": draft["envelope"]["body"]}, draft)
    return engine, originals, seen, replica_originals, replica_seen


def save(engine, **arguments):
    return engine.execute(6, {"kind": "tool_call", "tool": "honua_studio_save_version",
                              "arguments": {"draftId": "draft", "generation": 3, **arguments}})


def test_real_http_save_reads_original_draft_and_two_immutable_versions(http_map):
    engine, originals, seen, replica_originals, replica_seen = setup_map(http_map)
    result = save(engine)
    assert result['accepted'] is True
    assert seen == [(DRAFT_PATH, 'private-key'), (VERSION_PATH, 'private-key')]
    assert replica_seen == [(VERSION_PATH, 'private-key')]
    assert [row['slot'] for row in result['mapReadbacks']] == ['pre-save', 'saved-primary', 'saved-replica']
    for row, raw in zip(result['mapReadbacks'], [originals[DRAFT_PATH], originals[VERSION_PATH], replica_originals[VERSION_PATH]]):
        assert all(row['fieldMatches'].values())
        assert row['responseSha256'] == hashlib.sha256(raw).hexdigest()
        assert row['responseBytes'] == len(raw)
    assert all(engine.evidence['checks']['6'][key]['status'] == 'pass' for key in ('saved-map', 'replica-map'))


def test_consistent_composition_gets_feedback_then_model_selected_portable_update_can_save(http_map):
    engine, originals, seen, _, _ = setup_map(http_map, body={'layers': [], 'view': {}, 'widgets': []})
    with pytest.raises(ActionRejected, match='authored portable map package'):
        save(engine)
    engine.transport.tool.assert_not_called()
    row = engine.evidence['mapReadbacks'][0]
    assert row['fieldMatches']['body'] is False
    assert row['fieldMatches']['bodyMatchesDraft'] is True
    assert row['fieldMatches']['draftReceipt'] is True
    # Only an explicit model-selected update changes the candidate in this test.
    updated = copy.deepcopy(DRAFT)
    updated['generation'] = 4
    engine.transport.tool.return_value = {'structuredContent': updated}
    engine.execute(6, {'kind': 'tool_call', 'tool': 'honua_studio_update_draft', 'arguments': {
        'draftId': 'draft', 'generation': 3, 'body': copy.deepcopy(BODY)}})
    originals[DRAFT_PATH] = document(updated)
    engine.transport.tool.return_value = {'structuredContent': copy.deepcopy(SAVE)}
    save(engine, generation=4)
    assert len(engine.transport.tool.call_args_list) == 2
    assert seen.count((DRAFT_PATH, 'private-key')) == 2
    assert engine.evidence['checks']['6']['saved-map']['status'] == 'pass'


@pytest.mark.parametrize('field', ['draftId', 'itemId', 'generation', 'ownerId', 'workspaceId', 'packageKey', 'family', 'body'])
def test_original_write_vs_independent_draft_disagreement_is_execution_failure_not_model_feedback(http_map, field):
    engine, originals, _, _, _ = setup_map(http_map)
    value = copy.deepcopy(DRAFT)
    if field == 'body': value['envelope']['body'] = {'candidate-secret': 'never-public'}
    else: value[field] = 12 if field == 'generation' else 'different'
    originals[DRAFT_PATH] = document(value)
    with pytest.raises(ExecutionError, match='original write receipt') as error:
        save(engine)
    assert not isinstance(error.value, ActionRejected)
    engine.transport.tool.assert_not_called()
    assert 'never-public' not in json.dumps(engine.evidence['mapReadbacks'])


@pytest.mark.parametrize('arguments', [{'draftId': 'other'}, {'generation': 2}, {'generation': True}, {'generation': 3.0}])
def test_save_cannot_bind_other_draft_or_stale_generation(http_map, arguments):
    engine, _, seen, _, _ = setup_map(http_map)
    with pytest.raises(ActionRejected, match='latest observed draft'):
        save(engine, **arguments)
    assert not seen
    engine.transport.tool.assert_not_called()


@pytest.mark.parametrize('slot,field', [('primary', 'body'), ('replica', 'body'), ('primary', 'itemId'),
    ('replica', 'versionId'), ('primary', 'contentHash'), ('replica', 'family')])
def test_actual_post_save_or_replica_disagreement_stays_failed_with_exact_field_diagnostics(http_map, slot, field):
    engine, primary, _, replica, _ = setup_map(http_map)
    bad = copy.deepcopy(VERSION)
    if field in {'body', 'family'}: bad['envelope'][field] = {} if field == 'body' else 'dashboard'
    else: bad[field] = 'other'
    (primary if slot == 'primary' else replica)[VERSION_PATH] = document(bad)
    result = save(engine)
    check = engine.evidence['checks']['6']['saved-map' if slot == 'primary' else 'replica-map']
    assert check['status'] == 'fail'
    row = result['mapReadbacks'][1 if slot == 'primary' else 2]
    assert row['fieldMatches'][field] is False
    assert result['mapReadbacks'][0]['fieldMatches']['body'] is True
    assert row['fieldMatches']['bodyMatchesDraft'] is (field != 'body')
    assert engine.expected_map_body() == BODY


def test_native_ignoring_selected_create_or_update_body_is_execution_failure(http_map):
    engine, _, _, _, _ = setup_map(http_map)
    with pytest.raises(ExecutionError, match='selected body') as error:
        engine.record_map_draft('honua_studio_update_draft', {'draftId': 'draft', 'body': {}}, copy.deepcopy(DRAFT))
    assert not isinstance(error.value, ActionRejected)


def test_map_task_context_exposes_only_authored_portable_body_with_observed_layer_binding(http_map):
    engine, _, _, _, _ = setup_map(http_map)
    engine.fixture['mapBody']['sourceBindings'][0]['locator']['collectionId'] = '{layerId}'
    engine.resources['layerId'] = 23
    surface = engine.terminal_surface(6)['compositionContract']
    assert surface['authoredBody']['sourceBindings'][0]['locator']['collectionId'] == '23'
    assert engine.fixture['mapBody']['sourceBindings'][0]['locator']['collectionId'] == '{layerId}'
    assert surface['schemaVersion'] == '1.0'
    assert surface['currentDraft'] == {'draftId': 'draft', 'generation': 3, 'packageKey': 'journey-map'}
    assert 'different representation' in surface['requirement']
    assert engine.transport.tool.call_count == 0


@pytest.mark.parametrize('field', ['draftId', 'itemId', 'ownerId', 'workspaceId', 'packageKey', 'family'])
def test_mutation_cannot_replace_original_item_owner_or_workspace(http_map, field):
    engine, _, _, _, _ = setup_map(http_map)
    changed = copy.deepcopy(DRAFT)
    changed[field] = 'other'
    reason = 'content or canonical identity' if field == 'family' else 'original identity binding'
    with pytest.raises(ExecutionError, match=reason):
        engine.record_map_draft('honua_studio_update_draft', {'draftId': 'draft', 'body': BODY}, changed)


def test_saved_body_cannot_join_a_later_unrelated_saved_version(http_map):
    engine, _, _, _, _ = setup_map(http_map)
    engine.evidence['mapSaveInput'] = {**engine.evidence['mapDraftReceipt'], 'savedVersionId': 'different'}
    engine.resources.update(versionId='version', contentHash='native-hash')
    engine.check_map()
    assert engine.evidence['checks']['6']['saved-map']['status'] == 'fail'
    assert engine.evidence['mapReadbacks'][0]['fieldMatches']['body'] is True
    assert engine.evidence['mapReadbacks'][0]['fieldMatches']['bodyMatchesDraft'] is False


@pytest.mark.parametrize('field', ['ownerId', 'sourceDraftId', 'packageKey'])
def test_saved_version_must_come_from_same_original_draft_owner_and_package(http_map, field):
    engine, primary, _, _, _ = setup_map(http_map)
    changed = copy.deepcopy(VERSION)
    changed[field] = 'other'
    primary[VERSION_PATH] = document(changed)
    result = save(engine)
    assert engine.evidence['checks']['6']['saved-map']['status'] == 'fail'
    assert result['mapReadbacks'][1]['fieldMatches']['draftReceipt'] is False
    assert result['mapReadbacks'][1]['fieldMatches']['body'] is True


def test_unpaired_native_draft_can_omit_nullable_workspace_without_inventing_one(http_map):
    engine, primary, _, _, _ = setup_map(http_map)
    receipt = copy.deepcopy(DRAFT)
    del receipt['workspaceId']
    engine.record_map_draft('honua_studio_create_draft', {'body': BODY}, receipt)
    primary[DRAFT_PATH] = document(receipt)
    result = save(engine)
    assert result['mapReadbacks'][0]['fieldMatches']['draftReceipt'] is True
    assert engine.evidence['mapSaveInput']['workspaceId'] is None


def test_raw_credential_cannot_be_exposed_as_draft_update_package_binding(http_map):
    engine, _, _, _, _ = setup_map(http_map)
    changed = copy.deepcopy(DRAFT)
    changed['packageKey'] = 'map-private-key'
    with pytest.raises(ExecutionError, match='non-secret package identity'):
        engine.record_map_draft('honua_studio_create_draft', {'body': BODY}, changed)


def test_unavailable_published_layer_context_stays_blocked_without_invented_body(http_map):
    engine, _, seen, _, _ = setup_map(http_map)
    engine.fixture['mapBody']['sourceBindings'][0]['locator']['collectionId'] = '{layerId}'
    assert engine.terminal_surface(6) == {'compositionContract': {'available': False,
        'blockedBy': 'independently observed published layer identity'}}
    assert not seen


@pytest.mark.parametrize('version', ['', 'x' * 51, 'schema\nversion', 'private-key'])
def test_public_schema_observation_is_bounded_and_cannot_echo_credential(http_map, version):
    engine, primary, _, _, _ = setup_map(http_map)
    primary['/api/v1/studio/package-families'] = document({'families': [
        {'family': 'map', 'currentSchemaVersion': version}]})
    with pytest.raises(ExecutionError, match='bounded non-secret identity'):
        engine.terminal_surface(6)


@pytest.mark.parametrize('family', ['dashboard', 'app', None, 7, []])
def test_native_create_cannot_misidentify_resource_family_even_when_envelope_is_map(http_map, family):
    engine, _, _, _, _ = setup_map(http_map)
    wrong = copy.deepcopy(DRAFT)
    wrong['family'] = family
    with pytest.raises(ExecutionError, match='content or canonical identity'):
        engine.record_map_draft('honua_studio_create_draft', {'family': 'map', 'body': BODY}, wrong)
    assert engine.evidence['mapDraftReceipt']['family'] == 'map'


@pytest.mark.parametrize('family', ['dashboard', 'Dashboard', 'app'])
def test_model_selected_non_map_create_is_stage_feedback_before_any_candidate_write(http_map, family):
    # Stage 6 names map+dashboard, but its composition contract (terminal_surface) is the
    # portable map family only. A model that asks for another family gets invalid-action
    # feedback before the candidate is written; the map draft receipt is untouched.
    # Scheduled run 40349474852: a faithful dashboard create reached record_map_draft and
    # was reported as a candidate omission (infrastructure) instead.
    engine, _, _, _, _ = setup_map(http_map)
    before = copy.deepcopy(engine.evidence['mapDraftReceipt'])
    dashboard = copy.deepcopy(DRAFT)
    dashboard.update(draftId='dashboard-draft', itemId='dashboard-item', family=family)
    dashboard['envelope'] = {'family': family, 'body': {'widgets': []}}
    engine.transport.tool.return_value = {'structuredContent': dashboard}
    with pytest.raises(ActionRejected, match='composition contract is the map family'):
        engine.execute(6, {'kind': 'tool_call', 'tool': 'honua_studio_create_draft', 'arguments': {
            'family': family, 'packageKey': 'journey-dashboard', 'body': {'widgets': []}}})
    engine.transport.tool.assert_not_called()
    assert engine.evidence['mapDraftReceipt'] == before


def test_map_create_still_reaches_the_candidate_and_wrong_returned_family_stays_execution_failure(http_map):
    engine, _, _, _, _ = setup_map(http_map)
    wrong = copy.deepcopy(DRAFT)
    wrong['family'] = 'dashboard'
    engine.transport.tool.return_value = {'structuredContent': wrong}
    with pytest.raises(ExecutionError, match='content or canonical identity') as error:
        engine.execute(6, {'kind': 'tool_call', 'tool': 'honua_studio_create_draft', 'arguments': {
            'family': 'map', 'body': copy.deepcopy(BODY)}})
    assert not isinstance(error.value, ActionRejected)
    engine.transport.tool.assert_called_once()


def test_original_wrong_family_receipt_and_matching_actual_http_cannot_qualify_map(http_map):
    engine, primary, _, _, _ = setup_map(http_map)
    wrong = copy.deepcopy(DRAFT)
    wrong['family'] = 'dashboard'
    primary[DRAFT_PATH] = document(wrong)
    # A retained receipt from the earlier source must not make two matching,
    # consistently wrong family projections into proof of a map resource.
    engine.evidence['mapDraftReceipt']['family'] = 'dashboard'
    with pytest.raises(ExecutionError, match='original write receipt') as error:
        save(engine)
    assert not isinstance(error.value, ActionRejected)
    engine.transport.tool.assert_not_called()
    assert engine.evidence['mapReadbacks'][0]['fieldMatches']['family'] is False


def test_canonical_resource_family_comparison_preserves_native_enum_case(http_map):
    engine, primary, _, _, _ = setup_map(http_map)
    receipt = copy.deepcopy(DRAFT)
    receipt['family'] = 'Map'
    engine.record_map_draft('honua_studio_create_draft', {'family': 'map', 'body': BODY}, receipt)
    primary[DRAFT_PATH] = document(receipt)
    assert save(engine)['mapReadbacks'][0]['fieldMatches']['family'] is True


PUBLIC_PATH = '/api/v1/studio/published/maps/journey'


def public_map(engine, originals):
    engine.fixture['publishedPath'] = PUBLIC_PATH
    engine.evidence['approvalResolution'] = {'proposalId': 'actual-test-approval'}
    value = {key: copy.deepcopy(VERSION[key]) for key in ('itemId', 'versionId', 'contentHash', 'envelope')}
    value.update(publicationId='publication', route='/maps/journey', visibility='public')
    originals[PUBLIC_PATH] = document(value)
    return value


def test_public_dto_omits_private_draft_metadata_but_preserves_full_saved_content(http_map):
    engine, originals, seen, _, _ = setup_map(http_map)
    save(engine)
    public_map(engine, originals)
    assert engine.verify_final().status == 'pass'
    assert seen[-1] == (PUBLIC_PATH, None)
    row = engine.evidence['mapReadbacks'][-1]
    assert row['slot'] == 'published-route'
    assert all(row['fieldMatches'].values())
    assert not {'ownerId', 'packageKey', 'sourceDraftId', 'draftReceipt'} & row['fieldMatches'].keys()
    assert row['responseSha256'] == hashlib.sha256(originals[PUBLIC_PATH]).hexdigest()


@pytest.mark.parametrize('field', ['itemId', 'versionId', 'contentHash', 'family', 'body'])
def test_public_route_still_rejects_foreign_identity_family_or_actual_content(http_map, field):
    engine, originals, _, _, _ = setup_map(http_map)
    save(engine)
    public = public_map(engine, originals)
    if field in {'family', 'body'}: public['envelope'][field] = 'dashboard' if field == 'family' else {}
    else: public[field] = 'other'
    originals[PUBLIC_PATH] = document(public)
    assert engine.verify_final().status == 'fail'
    assert engine.evidence['mapReadbacks'][-1]['fieldMatches'][field] is False
    assert engine.expected_map_body() == BODY
