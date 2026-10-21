"""One normal role/key cohort for both original installed-client journeys.

Private material stays in exclusive mode600 journals outside public artifacts.
This coordinator never approves a gateway proposal or manufactures resources.
"""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent))
from mcp_deployment_identity_configuration import declaration,observe_configuration
from mcp_deployment_identity_role import ManagedRoleFixture,observe_role
from mcp_managed_identity_fixture import ManagedIdentityFixture,main as managed_main,profile_permissions
from mcp_execute_plan_state import redis_command
from mcp_map_package_validation import strict_json
from mcp_render_validation import inspect_candidate

CLIENTS=("sdk","inspector")
ROLES=("primary","foreign")


def candidate(runtime):
    return {key:runtime.get(key) for key in ("containerId","imageId","candidateSourceSha")}


def private_directory(path):
    path=Path(path)
    value=path.lstat()
    if (not path.is_absolute() or not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode)!=0o700
            or value.st_uid!=os.getuid() or path.resolve()!=path):
        raise ValueError("exclusive owned private cohort directory required")
    return path


def paths(private_root,state_path,client,role):
    if client not in CLIENTS or role not in ROLES:raise ValueError("unknown canonical cohort identity")
    return private_root/(client+"-"+role+".private.json"),state_path.with_name(state_path.name+"."+client+"-"+role+".receipt.json")


def persist(path,value,*,initial=False):
    path=Path(path)
    if initial:
        descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
        with os.fdopen(descriptor,"w") as stream:json.dump(value,stream,sort_keys=True,indent=2);stream.write("\n")
        return
    current=path.lstat()
    if (not stat.S_ISREG(current.st_mode) or current.st_uid!=os.getuid() or stat.S_IMODE(current.st_mode)!=0o600
            or current.st_nlink!=1):raise ValueError("owned public cohort receipt boundary differs")
    temporary=path.with_name(path.name+".writing-"+secrets.token_hex(8))
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0),0o600)
    try:
        with os.fdopen(descriptor,"w") as stream:json.dump(value,stream,sort_keys=True,indent=2);stream.write("\n")
        if path.lstat().st_ino!=current.st_ino:raise ValueError("cohort receipt replaced during update")
        os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()


def read_state(path):
    status=path.lstat()
    if (not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode)!=0o600 or status.st_uid!=os.getuid()
            or status.st_nlink!=1 or status.st_size>16*1024*1024):raise ValueError("owned public cohort receipt malformed")
    descriptor=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
    with os.fdopen(descriptor,"rb") as stream:
        if os.fstat(stream.fileno()).st_ino!=status.st_ino:raise ValueError("cohort receipt inode changed")
        return strict_json(stream.read(16*1024*1024+1))


def normal_role(args,configuration,configuration_observation,inspect):
    return ManagedRoleFixture(admin_credential=os.environ.get("HONUA_MCP_ADMIN_API_KEY"),
        configuration=configuration,configuration_observation=configuration_observation,inspect_owned=inspect,
        observe=lambda identifier:observe_role(identifier,os.environ.get("HONUA_CERT_DB_PASSWORD")))


def prepare(args,inspect):
    # Reject an invalid scoped grant before creating the shared normal role.
    profile_permissions("workspace-reader",args.service_id)
    root=private_directory(args.private_root);path=Path(args.state_path)
    configuration=declaration("mcp-deployment-"+args.nonce)
    runtime=inspect();observation=observe_configuration(configuration,inspect)
    state={"schema":"honua.mcp-deployment-managed-cohort/v1","nonce":args.nonce,"serviceId":args.service_id,
        "candidateImage":args.candidate_image,"candidate":candidate(runtime),"credentialSalt":args.credential_salt,
        "configuration":configuration,"configurationObservation":observation,"roleReceipt":None,
        "keys":{client:{} for client in CLIENTS},"failures":[],"cleanupComplete":False}
    persist(path,state,initial=True)
    role=None;created=[]
    try:
        role=normal_role(args,configuration,observation,inspect)
        state["roleReceipt"]=role.receipt;role.create();persist(path,state)
        for client in CLIENTS:
            for key_role in ROLES:
                private,public=paths(root,path,client,key_role)
                profile="admin" if key_role=="primary" else "workspace-reader"
                fixture=ManagedIdentityFixture(admin_credential=os.environ.get("HONUA_MCP_ADMIN_API_KEY"),
                    private_path=private,receipt_path=public,inspect_owned=inspect,redis=redis_command,
                    credential_salt=args.credential_salt,profile=profile,
                    **({"service_id":args.service_id} if key_role=="foreign" else {}))
                created.append((client,key_role,fixture));fixture.create()
                state["keys"][client][key_role+"Receipt"]=fixture.receipt;persist(path,state)
        if candidate(inspect())!=state["candidate"]:raise ValueError("candidate changed during managed cohort preparation")
        return state
    except Exception:
        state["failures"].append({"phase":"prepare"})
        # Cleanup only real established owned identities; incomplete attempts never pass.
        for client,key_role,fixture in reversed(created):
            try:fixture.revoke()
            except Exception:state["failures"].append({"phase":"prepare-key-cleanup","client":client,"role":key_role})
            state["keys"][client][key_role+"Receipt"]=fixture.receipt
        if role is not None and role.role_id is not None:
            try:role.revoke()
            except Exception:state["failures"].append({"phase":"prepare-role-cleanup"})
        raise
    finally:
        if role is not None:state["roleReceipt"]=role.receipt
        persist(path,state)


def close(args,inspect):
    root=private_directory(args.private_root);path=Path(args.state_path);state=read_state(path)
    if (not isinstance(state,dict) or state.get("schema")!="honua.mcp-deployment-managed-cohort/v1"
            or state.get("nonce")!=args.nonce or state.get("serviceId")!=args.service_id
            or state.get("credentialSalt")!=args.credential_salt or state.get("candidateImage")!=args.candidate_image
            or state.get("candidate")!=candidate(inspect()) or state.get("configuration")!=declaration("mcp-deployment-"+args.nonce)
            or state.get("cleanupComplete") is not False or not isinstance(state.get("failures"),list)):
        raise ValueError("original managed cohort candidate/configuration differs")
    from mcp_deployment_identity_role import stamp
    state["cleanupStartedAt"]=stamp()
    role=None
    try:
        # Each key uses its own original private/public journal, source profile,
        # effective permission and independent Redis ownership checks before revoke.
        for client in CLIENTS:
            for key_role in ROLES:
                private,public=paths(root,path,client,key_role)
                profile="admin" if key_role=="primary" else "workspace-reader"
                command=["revoke","--profile",profile,"--private-path",str(private),"--receipt-path",str(public),
                    "--credential-salt",args.credential_salt,"--candidate-source-sha",args.candidate_source_sha,
                    "--candidate-image",args.candidate_image,"--owned-disposable-candidate"]
                if key_role=="foreign":command.extend(["--service-id",args.service_id])
                # The helper emits only its original public receipt; never retain
                # unbounded transport diagnostics or private fixture object state.
                try:
                    with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):result=managed_main(command)
                    if result:raise ValueError("normal managed cleanup refused")
                except Exception:
                    state["failures"].append({"phase":"key-cleanup","client":client,"role":key_role})
                try:
                    if public.exists():state["keys"][client][key_role+"Receipt"]=read_state(public)
                except Exception:
                    state["failures"].append({"phase":"key-receipt-readback","client":client,"role":key_role})
                try:persist(path,state)
                except Exception:
                    # A receipt write failure must not strand the other owned keys.
                    state["failures"].append({"phase":"key-receipt-persistence","client":client,"role":key_role})
        role=normal_role(args,state["configuration"],state["configurationObservation"],inspect)
        original=state.get("roleReceipt")
        if not isinstance(original,dict):raise ValueError("owned role receipt missing")
        role.receipt=original;role.role_id=original.get("roleId")
        role.grants=role.receipt.get("configuredStore",{}).get("value",{}).get("permissions",[])
        originals=[]
        for filename in (args.sdk_journey,args.inspector_journey):
            if filename is None:break
            source=Path(filename)
            if source.is_symlink() or not source.is_file() or source.stat().st_size>2*1024*1024:
                raise ValueError("bounded original client journey missing")
            originals.append(source.read_bytes())
        try:role.bind_journeys(originals)
        except Exception:state["failures"].append({"phase":"shared-journey-binding"})
    except Exception:
        state["failures"].append({"phase":"cohort-cleanup"})
    finally:
        if role is not None:
            try:role.revoke()
            except Exception:state["failures"].append({"phase":"role-cleanup"})
            state["roleReceipt"]=role.receipt
        state["cleanupComplete"]=(not state["failures"] and role is not None and role.receipt.get("cleanupComplete") is True
            and all(state["keys"].get(client,{}).get(key_role+"Receipt",{}).get("cleanupComplete") is True for client in CLIENTS for key_role in ROLES))
        persist(path,state)
    return state


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("prepare","close"))
    for name in ("private-root","state-path","nonce","service-id","credential-salt","candidate-source-sha","candidate-image"):
        parser.add_argument("--"+name,required=True)
    parser.add_argument("--sdk-journey");parser.add_argument("--inspector-journey")
    parser.add_argument("--owned-disposable-candidate",action="store_true",required=True)
    args=parser.parse_args(argv)
    if not re.fullmatch(r"[a-f0-9]{12}",args.nonce) or not re.fullmatch(r"[a-f0-9]{32}",args.credential_salt):
        print("managed deployment cohort input refused",file=sys.stderr);return 1
    inspect=lambda:inspect_candidate(args.candidate_source_sha,args.candidate_image)
    try:
        state=prepare(args,inspect) if args.action=="prepare" else close(args,inspect)
        print(json.dumps(state,allow_nan=False))
        return 0 if args.action=="prepare" or state["cleanupComplete"] else 1
    except Exception:
        print("normal owned deployment identity cohort refused; retain its public partial receipt",file=sys.stderr);return 1

if __name__=="__main__":raise SystemExit(main())
