import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import resolve_trunk_candidate as resolver
from test_resolve_trunk_candidate import Declarations, DeclaredTrunk, NEW, declaration_bytes


@pytest.mark.parametrize("relative_path", [resolver.SERVER_COMPONENT_VERSIONS_PATH, resolver.COMPONENT_VERSIONS_PATH])
def test_server_declaration_supports_current_and_pinned_legacy_layouts(relative_path):
    source = Declarations({('honua-io/honua-server', NEW, relative_path): declaration_bytes('honua-server')})
    versions, path = resolver.component_version_declaration(source, 'honua-server', {
        'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW})
    assert versions['contractVersions'] == {'admin': 'v1'}
    assert path == relative_path


def test_invalid_current_server_declaration_refuses_despite_valid_legacy():
    source = Declarations({
        ('honua-io/honua-server', NEW, resolver.SERVER_COMPONENT_VERSIONS_PATH): b'{',
        ('honua-io/honua-server', NEW, resolver.COMPONENT_VERSIONS_PATH): declaration_bytes('honua-server')})
    with pytest.raises(resolver.ResolutionError, match='is not a JSON document'):
        resolver.component_versions(source, 'honua-server', {
            'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW})
    assert source.reads == [('honua-io/honua-server', NEW, resolver.SERVER_COMPONENT_VERSIONS_PATH)]


def test_current_server_declaration_wins_over_legacy():
    source = Declarations({
        ('honua-io/honua-server', NEW, resolver.SERVER_COMPONENT_VERSIONS_PATH): declaration_bytes('honua-server'),
        ('honua-io/honua-server', NEW, resolver.COMPONENT_VERSIONS_PATH): b'{'})
    versions, path = resolver.component_version_declaration(source, 'honua-server', {
        'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW})
    assert versions['contractVersions'] == {'admin': 'v1'}
    assert path == resolver.SERVER_COMPONENT_VERSIONS_PATH
    assert len(source.reads) == 1


def test_unreadable_current_server_declaration_does_not_try_legacy():
    class Unreadable:
        def file(self, repository, revision, path):
            assert path == resolver.SERVER_COMPONENT_VERSIONS_PATH
            raise resolver.ResolutionError('permission denied', status=403)

    with pytest.raises(resolver.ResolutionError, match='permission denied'):
        resolver.component_versions(Unreadable(), 'honua-server', {
            'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW})



def test_missing_both_server_declarations_refuses_and_records_both_paths_in_walk():
    source = DeclaredTrunk({})
    component = {'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW}
    with pytest.raises(resolver.MissingDeclaration) as refusal:
        resolver.component_versions(source, 'honua-server', component)
    assert refusal.value.status == 404
    assert refusal.value.paths == (resolver.SERVER_COMPONENT_VERSIONS_PATH, resolver.COMPONENT_VERSIONS_PATH)
    assert source.reads == [
        ('honua-io/honua-server', NEW, resolver.SERVER_COMPONENT_VERSIONS_PATH),
        ('honua-io/honua-server', NEW, resolver.COMPONENT_VERSIONS_PATH)]

    walk = {}
    with pytest.raises(resolver.ResolutionError, match='no qualifying trunk commit'):
        resolver.select_component('honua-server', component, source, None, 1, walk,
            declare=lambda row: resolver.component_versions(source, 'honua-server', row))
    assert walk['skipped'] == [{'sha': NEW, 'reason':
        f'no {resolver.SERVER_COMPONENT_VERSIONS_PATH} or {resolver.COMPONENT_VERSIONS_PATH} at this commit'}]
    assert walk.get('aborted') is None


def test_missing_new_then_forbidden_legacy_server_declaration_refuses():
    class ForbiddenLegacy(Declarations):
        def file(self, repository, revision, path):
            if path == resolver.COMPONENT_VERSIONS_PATH:
                raise resolver.ResolutionError('legacy permission denied', status=403)
            return super().file(repository, revision, path)

    with pytest.raises(resolver.ResolutionError, match='legacy permission denied') as refusal:
        resolver.component_versions(ForbiddenLegacy({}), 'honua-server', {
            'repository': 'https://github.com/honua-io/honua-server', 'sha': NEW})
    assert not isinstance(refusal.value, resolver.MissingDeclaration)


def test_server_without_immutable_revision_names_the_canonical_declaration():
    with pytest.raises(resolver.ResolutionError, match=resolver.SERVER_COMPONENT_VERSIONS_PATH):
        resolver.component_versions(Declarations({}), 'honua-server', {
            'repository': 'https://github.com/honua-io/honua-server', 'sha': 'trunk'})
