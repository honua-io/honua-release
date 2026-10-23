"""Verify immutable public worker inputs; no controller/oracle data or secrets."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil

from agent_e2e_errors import PackError


def require(condition, reason):
    if not condition:
        raise PackError(reason)


def digest(raw):
    return 'sha256:'+hashlib.sha256(raw).hexdigest()


def verify(root):
    root = Path(root).resolve(strict=True)
    manifest = json.loads((root/'public-manifest.json').read_bytes())
    require(set(manifest) == {'schemaVersion', 'knowledge', 'files'} and manifest['schemaVersion'] == 1,
            'unsupported public worker bundle')
    knowledge = manifest['knowledge']
    require(set(knowledge) == {'schemaVersion', 'sourceRepository', 'sourceRevision', 'bundleDigest',
            'graphDigest', 'normalization', 'files'} and knowledge['schemaVersion'] == 1 and
            knowledge['normalization'] == 'UTF-8/LF', 'unsupported public knowledge manifest')
    require(json.loads((root/'knowledge/knowledge-manifest.json').read_bytes()) == knowledge,
            'public knowledge manifest differs')
    seen = {'public-manifest.json', 'knowledge/knowledge-manifest.json'}
    for group, rows in (('knowledge', knowledge['files']), ('inputs', manifest['files'])):
        require(isinstance(rows, list) and rows, 'empty public knowledge/input inventory')
        for row in rows:
            require(set(row) == {'path', 'sha256', 'bytes'} and type(row['bytes']) is int and row['bytes'] >= 0,
                    'invalid public input descriptor')
            relative = row['path']
            require(isinstance(relative, str) and relative and ':' not in relative and '\\' not in relative and
                    not relative.startswith('/') and all(part not in ('', '.', '..') for part in relative.split('/')),
                    'public input path escapes bundle')
            path = root/group/relative
            require(path.is_file() and not any(part.is_symlink() for part in (path, *path.parents)) and
                    path.resolve().is_relative_to(root), 'public input missing or symlinked')
            name = group+'/'+relative
            require(name not in seen, 'duplicate public worker input')
            raw = path.read_bytes()
            require(len(raw) == row['bytes'] and digest(raw) == row['sha256'], 'public worker input bytes changed')
            seen.add(name)
    actual = {path.relative_to(root).as_posix() for path in root.rglob('*') if path.is_file()}
    require(actual == seen, 'unreviewed files in public worker bundle')
    graph_raw = (root/'knowledge/okf/domain-graph.v1.json').read_bytes()
    graph = json.loads(graph_raw)
    require(digest(graph_raw) == knowledge['graphDigest'] and graph.get('executionVerified') is False and
            graph.get('bundleDigest') == knowledge['bundleDigest'], 'public knowledge graph differs')
    return manifest


def prepare(root='/opt/honua-public', workspace='/workspace'):
    """Fixed trusted startup command; verify bytes before exposing selected files."""
    root, workspace = Path(root), Path(workspace).resolve(strict=True)
    manifest = verify(root)
    destinations = [workspace/row['path'] for row in manifest['files']]
    require(all(not path.exists() and not path.is_symlink() for path in destinations),
            'public input setup already occurred; refuse overwrite')
    for row, path in zip(manifest['files'], destinations):
        require(not any(parent.is_symlink() for parent in path.parents) and
                path.resolve().is_relative_to(workspace), 'public input destination escapes workspace')
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root/'inputs'/row['path'], path)
        require(digest(path.read_bytes()) == row['sha256'], 'copied public input differs')
        path.chmod(0o444)
    knowledge = {key: manifest['knowledge'][key] for key in
        ('sourceRepository', 'sourceRevision', 'bundleDigest', 'graphDigest')}
    knowledge['manifestDigest'] = digest(json.dumps(manifest['knowledge'], sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode())
    return {'knowledgeBinding': knowledge, 'files': [
        {key: row[key] for key in ('path', 'sha256')} for row in manifest['files']],
        'documentationRoot': str(root/'knowledge'), 'publicBundleDigest': digest((root/'public-manifest.json').read_bytes())}
