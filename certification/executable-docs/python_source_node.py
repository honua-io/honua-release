"""The existing governed Node prerequisite, read-only in the exact dev reader only."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

IMAGE="node:22-bookworm@sha256:363e1587494626837fa7f9a23bdb453d13b0ff3c67c705c2805cfc69c2d2fad7"
CITATION="README.md:279-281 — the test suite needs Node.js with node on PATH"
LABEL="honua.python-source-node.owner"


def digest(data): return hashlib.sha256(data).hexdigest()


def require_node(declaration, selection):
    dev=selection["documentation"][1]
    if dev["document"]!="README.md" or dev["index"]!=8:
        raise ValueError("Node prerequisite is not the exact selected development context")
    actual={key:declaration[key] for key in ("document","blockIndex","blockSha256")}
    expected={"document":dev["document"],"blockIndex":dev["index"],"blockSha256":dev["sha256"]}
    if actual["document"]=="README.md" and actual["blockIndex"]==8 and actual!=expected:
        raise ValueError("Node development prerequisite differs from the selected original block")
    return actual==expected


def census(prefix):
    prefix=Path(prefix)
    if prefix.is_symlink() or not prefix.is_dir(): raise ValueError("Node prefix is not an owned actual directory")
    rows={}
    for path in sorted(prefix.rglob("*")):
        info=path.lstat(); name=str(path.relative_to(prefix))
        if stat.S_ISLNK(info.st_mode):
            if not path.resolve(strict=True).is_relative_to(prefix.resolve()):
                raise ValueError("Node prerequisite symlink escapes its fixed prefix")
            rows[name]={"kind":"symlink","target":os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):
            rows[name]={"kind":"file","mode":stat.S_IMODE(info.st_mode),"sha256":digest(path.read_bytes())}
        elif stat.S_ISDIR(info.st_mode):
            rows[name]={"kind":"directory","mode":stat.S_IMODE(info.st_mode)}
        else: raise ValueError("Node prerequisite contains an unsupported object")
    node=rows.get("bin/node",{})
    if node.get("kind")!="file" or node.get("mode",0)&0o111==0:
        raise ValueError("Node prerequisite lacks its actual executable")
    return rows


def verify(prefix, receipt):
    actual=census(prefix)
    if receipt.get("image")!=IMAGE or actual!=receipt.get("files"):
        raise ValueError("governed Node prerequisite bytes or modes changed")
    return {"image":IMAGE,"filesSha256":digest(json.dumps(actual,sort_keys=True).encode()),
            "nodeSha256":actual["bin/node"]["sha256"],"citation":CITATION}


def prepare(directory: Path, sources: Path):
    configuration=sources.read_bytes()
    if json.loads(configuration)["runtimes"]["node"]!=IMAGE:
        raise ValueError("Node prerequisite differs from the already governed runtime")
    directory.mkdir(mode=0o700)
    prefix=directory/"prefix"
    (prefix/"bin").mkdir(parents=True)
    (prefix/"lib").mkdir()
    commands=[]
    def save(name,value):
        with (directory/name).open("x") as stream: json.dump(value,stream,sort_keys=True)
    def command(argv):
        ordinal=len(commands)
        actual=subprocess.run(argv,capture_output=True,check=False)
        for name in ("stdout","stderr"):
            with (directory/f"command-{ordinal}.{name}").open("xb") as stream: stream.write(getattr(actual,name))
        row={"argv":argv,"exitCode":actual.returncode,"stdoutSha256":digest(actual.stdout),"stderrSha256":digest(actual.stderr)}
        save(f"command-{ordinal}.json",row);commands.append(row)
        if actual.returncode: raise ValueError("governed Node prerequisite command failed; retain raw streams")
        return actual.stdout
    command(["docker","pull","--platform","linux/amd64",IMAGE])
    image=json.loads(command(["docker","image","inspect","--format",'{"id":{{json .Id}},"repoDigests":{{json .RepoDigests}},"os":{{json .Os}},"architecture":{{json .Architecture}},"env":{{json .Config.Env}}}',IMAGE]))
    if ("node@"+IMAGE.split("@")[1] not in image["repoDigests"] or image["os"]!="linux" or image["architecture"]!="amd64"):
        raise ValueError("actual Node image differs from the governed immutable runtime")
    owner=uuid.uuid4().hex
    container=command(["docker","create","--platform","linux/amd64","--label",LABEL+"="+owner,IMAGE]).decode().strip()
    if len(container)!=64 or any(char not in "0123456789abcdef" for char in container): raise ValueError("Node source container ID is malformed")
    save("request.json",{"containerId":container,"ownerNonce":owner,"image":IMAGE})
    def observe():
        result=json.loads(command(["docker","inspect","--format",'{"id":{{json .Id}},"image":{{json .Image}},"status":{{json .State.Status}},"running":{{json .State.Running}},"labels":{{json .Config.Labels}}}',container]))
        if result["id"]!=container or result["image"]!=image["id"] or result["labels"].get(LABEL)!=owner:
            raise ValueError("Node source container authority changed")
        return result
    try:
        before=observe();save("before.json",before)
        if before["status"]!="created" or before["running"]: raise ValueError("Node copy source unexpectedly ran")
        command(["docker","cp",container+":/usr/local/bin/node",str(prefix/"bin/node")])
        command(["docker","cp",container+":/usr/local/lib/node_modules",str(prefix/"lib")])
        for name,target in (("npm","npm-cli.js"),("npx","npx-cli.js")):
            (prefix/"bin"/name).symlink_to("../lib/node_modules/npm/bin/"+target)
        receipt={"image":IMAGE,"observedImage":image,"sourcesSha256":digest(configuration),"citation":CITATION,
                 "files":census(prefix),"sourceAdmitted":False,"publishedWheelIdentity":False,"candidateQualified":False}
        save("receipt.json",receipt)
        verify(prefix,receipt)
        return prefix,receipt
    finally:
        observed=observe();save("cleanup-observed.json",observed)
        if observed["status"]=="created" and not observed["running"]:
            removed=command(["docker","rm",container]).decode().strip()
            save("cleanup.json",{"containerId":container,"removed":removed==container})
        else: save("cleanup-pending.json",observed)
