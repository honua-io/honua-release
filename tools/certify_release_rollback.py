#!/usr/bin/env python3
"""Execute restart/success and injected mixed-state rollback certifications."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import release_rollback as rollback  # noqa: E402


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return path


def fixture(root: Path, name: str):
    a = {"components":{"server":{"artifact":"sha256:"+"a"*64},"worker":{"artifact":"sha256:"+"b"*64}},"contentDigests":{"config":"sha256:"+"c"*64,"capability":"sha256:"+"d"*64},"schema":"106","rollbackCompatibility":{"schemaVersions":["107"]}}
    b = {"components":{"server":{"artifact":"sha256:"+"e"*64},"worker":{"artifact":"sha256:"+"f"*64}},"contentDigests":{"config":"sha256:"+"1"*64,"capability":"sha256:"+"2"*64},"schema":"107","rollbackCompatibility":{"schemaVersions":["107"]}}
    ap, bp = write(root/f"{name}-lock-a.json",a), write(root/f"{name}-lock-b.json",b)
    planes=[]
    for ident,kind,provider,path in [("serving-east","serving","deploy/east","/components/server/artifact"),("serving-west","serving","deploy/west","/components/server/artifact"),("worker-default","worker","queue/default","/components/worker/artifact"),("config","config","projection/config","/contentDigests/config"),("capability","capability","projection/capability","/contentDigests/capability")]:
        planes.append({"id":ident,"kind":kind,"providerId":provider,"lockPath":path,"current":rollback.pointer(b,path)})
    env=write(root/f"{name}-environment.json",{"name":name,"currentLockDigest":rollback.digest(bp),"planes":planes,"schema":{"lockPath":"/schema"}})
    return ap,bp,env


def main(argv=None):
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True); args=parser.parse_args(argv)
    args.output.mkdir(parents=True,exist_ok=True)
    a,b,env=fixture(args.output,"success")
    rollback.run(environment_path=env,from_path=b,to_path=a,store=args.output/"success-store",receipt_path=args.output/"success-receipt.interrupted.json",stop_after=2)
    success=rollback.run(environment_path=env,from_path=b,to_path=a,store=args.output/"success-store",receipt_path=args.output/"success-receipt.json")
    a2,b2,env2=fixture(args.output,"mixed")
    mixed=rollback.run(environment_path=env2,from_path=b2,to_path=a2,store=args.output/"mixed-store",receipt_path=args.output/"mixed-state-receipt.json",fail_plane="serving-west")
    if success["status"]!="Succeeded" or mixed["status"]!="ManualInterventionRequired": return 1
    write(args.output/"summary.json",{"successOperation":success["id"],"mixedOperation":mixed["id"],"success":"Succeeded","negative":"ManualInterventionRequired"})
    return 0


if __name__=="__main__": raise SystemExit(main())
