"""Export only reviewed runtime files into a fresh worker image build context."""
from pathlib import Path
import argparse
import hashlib
import json


RUNTIME = ("agent_e2e_errors.py", "agent_e2e_worker.py", "agent_e2e_container.py", "agent_e2e_container_entry.py")


def bundle(destination):
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
    return {"schemaVersion": 1, "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(bundle(args.output), sort_keys=True))
