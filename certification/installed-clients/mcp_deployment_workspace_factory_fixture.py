"""Explicit offline CLI observations for actual JS factory/wire controls.

The production progress/final oracles always execute unchanged. Normal managed
fixture implementations construct original API/store receipts through their
controlled offline adapters. No successful authority judgment is substituted.
"""
from __future__ import annotations
import base64
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys


def main():
    args=sys.argv[1:]
    assert args[:2]==['-I','-B']
    name=Path(args[2]).name
    original_python=os.environ['OFFLINE_ORIGINAL_PYTHON']
    # These are the actual protected progress/judge modules, never responses.
    if name in ('mcp_deployment_workspace_cohort.py','mcp_deployment_workspace_validation.py'):
        os.execv(original_python,[original_python,*args])
    directory=Path(os.environ['OFFLINE_COHORT_DIRECTORY'])
    sys.path.insert(0,str(directory))
    import mcp_deployment_workspace_full_controls as vectors
    import mcp_deployment_identity_role as role
    import mcp_deployment_workspace_cohort as full
    storage=directory/'offline-full-cohort.json'
    clock=directory/'offline-clock.json'
    runtime_path=directory/'offline-runtime.json'
    commands=directory/'offline-helper-commands.jsonl'
    with commands.open('a') as stream:stream.write(json.dumps({'helper':name,'args':args})+'\n')
    def option(key):return args[args.index(key)+1]
    def advance(value):
        old=json.loads(clock.read_text())
        if datetime.fromisoformat(value.replace('Z','+00:00'))>datetime.fromisoformat(old.replace('Z','+00:00')):
            clock.write_text(json.dumps(value))
    if name=='mcp_deployment_identity_fixture.py' and args[3]=='prepare':
        vectors.NONCE=option('--nonce');vectors.keys.SALT=option('--credential-salt')
        vector=vectors.full_controls()
        preparation=full.original_preparation(vector['terminal'])
        storage.write_text(json.dumps(vector))
        root=Path(option('--private-root'))
        for index,(selector,slot,profile) in enumerate((
                ('sdk','primary','admin'),('sdk','foreign','workspace-reader'),
                ('inspector','primary','admin'),('inspector','foreign','workspace-reader')),1):
            receipt=preparation['keys'][selector][slot+'Receipt']
            key='hnua_'+base64.urlsafe_b64encode(bytes([index])*32).decode().rstrip('=')
            journal={'key':key,'keyId':receipt['keyId'],'name':receipt['name'],'profile':profile,
                'credentialSalt':preparation['credentialSalt'],'candidate':receipt['candidate'],
                'receiptSha256':hashlib.sha256(json.dumps(receipt,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
            path=root/(selector+'-'+slot+'.private.json');path.write_text(json.dumps(journal));path.chmod(0o600)
        advance(vectors.at(2));runtime_path.write_text(json.dumps({'restarts':0,'runtime':vector['members']['sdk']['semantic']['runtimeBefore']}))
        print(json.dumps(preparation));return
    vector=json.loads(storage.read_text());members=vector['members'];state=json.loads(runtime_path.read_text())
    if name=='mcp_deployment_identity_fixture.py' and args[3]=='close':
        terminal=vector['terminal']
        originals=[Path(option('--'+selector+'-journey')).read_bytes() for selector in ('sdk','inspector')]
        terminal['roleReceipt']['cohort']['journeys']=role.journey_bindings(originals,
            terminal['candidate'],terminal['roleReceipt']['configurationSha256'],terminal['nonce'])
        # Real pure role checks rejudge the original actual factory envelopes.
        role.verify_role_receipt(terminal['roleReceipt'],terminal['configuration'],terminal['configurationObservation'],members['inspector']['semantic']['runtimeAfter'])
        for path in Path(option('--private-root')).glob('*.private.json'):path.unlink()
        advance(vectors.at(1820));print(json.dumps(terminal));return
    if name=='mcp_render_validation.py' and '--inspect-runtime' in args:
        print(json.dumps(state['runtime']));return
    if name=='mcp_deployment_workspace_state.py' and '--restart' in args:
        selector=('sdk','inspector')[state['restarts']]
        state={'restarts':state['restarts']+1,'runtime':members[selector]['semantic']['runtimeAfter']}
        runtime_path.write_text(json.dumps(state));advance(state['runtime']['startedAt']);print(json.dumps(state['runtime']));return
    if name=='mcp_deployment_workspace_prerequisites.py':
        label=option('--label')
        for value in members.values():
            for observed in (value['semantic']['workspacePrerequisite'],value['authority']['foreignWorkspacePrerequisite'],value['authority']['personalWorkspacePrerequisite']):
                if observed['label']==label:
                    advance(observed['output']['completedAt']);print(json.dumps(observed));return
        raise AssertionError('unknown offline owned GP label')
    if name=='mcp_ops_events_data_read.py':
        credential=os.environ['HONUA_MCP_API_KEY']
        selector='sdk' if credential=='hnua_'+base64.urlsafe_b64encode(bytes([2])*32).decode().rstrip('=') else 'inspector'
        observed=members[selector]['authority']['limitedRead'];advance(observed['completedAt']);print(json.dumps(observed));return
    if name=='mcp_deployment_workspace_state.py':
        for index,(selector,value) in enumerate(members.items()):
            if '--proposal-id' in args:
                if option('--proposal-id')==value['semantic']['proposalBefore']['proposalId']:
                    print(json.dumps(value['semantic']['proposalAfter' if state['restarts']>index else 'proposalBefore']));return
            else:
                after=state['restarts']>index
                candidates=[value['semantic']['storeAfter' if after else 'storeBefore'],
                    value['authority']['storeAfter' if after else 'storeBefore'],value['authority']['personalStoreAfter' if after else 'personalStoreBefore']]
                for observed in candidates:
                    if observed['workspaceId']==option('--workspace-id'):
                        advance(observed['completedAt']);print(json.dumps(observed));return
        raise AssertionError('unknown independently observed offline owned store')
    raise AssertionError('unlisted offline helper '+name)


if __name__=='__main__':main()
