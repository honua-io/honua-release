"""Export only reviewed runtime files into a fresh worker image build context."""
from pathlib import Path
import argparse
import hashlib
import json


RUNTIME = ("agent_e2e_errors.py", "agent_e2e_worker.py", "agent_e2e_container.py", "agent_e2e_container_entry.py",
           "agent_e2e_mcp.py", "agent_e2e_mcp_runner.py", "agent_e2e_mcp_entry.py",
           "agent_e2e_public.py", "agent_e2e_public_entry.py")


def bundle(destination, *, knowledge_root=None, pack_path=None, case_id=None):
    tools = Path(__file__).resolve().parent
    image = tools.parent / "certification" / "agent-e2e" / "worker"
    sources = {name: tools/name for name in RUNTIME}
    sources.update({name: image/name for name in ("Dockerfile", "requirements.txt")})
    target = Path(destination)
    target.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, source in sorted(sources.items()):
        content = source.read_bytes().replace(b"\r\n", b"\n")
        (target/name).write_bytes(content)
        files[name] = "sha256:" + hashlib.sha256(content).hexdigest()
    public = target/'public'
    public.mkdir()
    supplied = (knowledge_root, pack_path, case_id)
    if any(value is not None for value in supplied):
        if not all(value is not None for value in supplied):
            raise ValueError('public bundles require knowledge root, frozen pack and exact case')
        from agent_e2e_knowledge import binding, verify
        from agent_e2e_pack import export_inputs, load_json, select
        root, pack_path = Path(knowledge_root), Path(pack_path)
        manifest, pack = load_json(root/'knowledge-manifest.json'), load_json(pack_path)
        verify(root, manifest, binding(manifest))
        case, _ = select(pack, case_id)
        if case['variant'] not in pack['developmentVariants']:
            raise ValueError('development worker image cannot expose held-out C inputs')
        import shutil
        shutil.copytree(root, public/'knowledge')
        corpus_path = pack_path.parent/pack['corpusManifest']
        corpus = load_json(corpus_path)
        index = export_inputs(pack, case_id, corpus_path.parent, corpus, public/'inputs')
        sizes = {row['path']: row['bytes'] for row in corpus['files']}
        public_manifest = {'schemaVersion': 1, 'knowledge': manifest,
            'files': [{**row, 'bytes': sizes[row['path']]} for row in index]}
        (public/'public-manifest.json').write_text(json.dumps(public_manifest, sort_keys=True)+'\n')
        from agent_e2e_public import verify as verify_public
        verify_public(public)
        for path in public.rglob('*'):
            if path.is_file():
                files[path.relative_to(target).as_posix()] = 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
    return {"schemaVersion": 1, "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument('--knowledge-root')
    parser.add_argument('--pack', type=Path)
    parser.add_argument('--case-id')
    args = parser.parse_args()
    print(json.dumps(bundle(args.output, knowledge_root=args.knowledge_root, pack_path=args.pack,
                            case_id=args.case_id), sort_keys=True))
