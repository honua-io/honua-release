// Explicit offline HTTP/client fixtures; never installed-client/candidate proof.
import assert from "node:assert/strict";
import test from "node:test";
import { createServer } from "node:http";
import { createHash, randomUUID } from "node:crypto";
import { mkdtempSync, copyFileSync, readFileSync, writeFileSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { spawnSync } from "node:child_process";
import { observeCandidateRequest } from "./mcp_candidate_wire.mjs";
import { prepareControlledInstallation, controlCredential } from "./mcp_map_package_wire_controls.mjs";
const here=fileURLToPath(new URL("./",import.meta.url));
const salt="a".repeat(32), tenant="mcp-deployment-123456abcdef";
const params={uri:"honua://workspaces/owned-offline"};
const sha=bytes=>createHash("sha256").update(bytes).digest("hex");

async function candidate() {
  const seen=[];
  const server=createServer(async(incoming,outgoing)=>{
    if(incoming.method==="DELETE"){seen.push({method:"DELETE",tenant:incoming.headers["x-honua-tenant"]});outgoing.writeHead(200).end();return;}
    const chunks=[];for await(const chunk of incoming)chunks.push(chunk);
    const message=JSON.parse(Buffer.concat(chunks));seen.push({method:message.method,tenant:incoming.headers["x-honua-tenant"]});
    const initializing=message.method==="initialize";
    outgoing.writeHead(200,{"content-type":"application/json",...(initializing?{"mcp-session-id":randomUUID()}:{})});
    outgoing.end(JSON.stringify({jsonrpc:"2.0",id:message.id,result:initializing?
      {protocolVersion:"2025-06-18",capabilities:{},serverInfo:{name:"explicit-offline",version:"0.0.0"}}:
      {contents:[{uri:params.uri,mimeType:"application/json",text:'{"owned":true}'}]}}));
  });
  await new Promise((resolve,reject)=>{server.once("error",reject);server.listen(8080,"127.0.0.1",resolve);});
  return {seen,close:async()=>{server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}};
}

function installation() {
  const directory=mkdtempSync(join(tmpdir(),"release-explicit-tenant-controls-"));
  for(const name of ["mcp_fixed_client_launcher.mjs","mcp_fixed_sdk_runner.mjs","mcp_fixed_client_manifest.v1.json",
    "mcp_map_package_validation.py","mcp_render_validation.py","render_png.py"])copyFileSync(join(here,name),join(directory,name));
  const manifestPath=join(directory,"mcp_fixed_client_manifest.v1.json");
  const root=prepareControlledInstallation(directory,manifestPath);
  const manifest=JSON.parse(readFileSync(manifestPath));const selected=manifest.clients.inspector;
  const packageRoot=join(root,...selected.name.split("/"));
  const sources={"package.json":JSON.stringify({name:selected.name,version:selected.version,type:"module"}),
    [selected.entry]:`// Explicit offline public-CLI header grammar fixture, not the published Inspector.
const args=process.argv.slice(2), endpoint=args[args.indexOf('--cli')+1];
const headers={'content-type':'application/json'};const at=args.indexOf('--header');
if(at>=0)for(let i=at+1;i<args.length&&!args[i].startsWith('--');i++){const n=args[i].indexOf(':');if(n<1)throw Error('bad header pair');headers[args[i].slice(0,n)]=args[i].slice(n+1).trim();}
const initial=await fetch(endpoint,{method:'POST',headers,body:JSON.stringify({jsonrpc:'2.0',id:1,method:'initialize',params:{protocolVersion:'2025-06-18',capabilities:{},clientInfo:${JSON.stringify(selected.clientInfo)}}})});await initial.json();
const response=await fetch(endpoint,{method:'POST',headers:{...headers,'mcp-session-id':initial.headers.get('mcp-session-id')},body:JSON.stringify({jsonrpc:'2.0',id:2,method:'resources/read',params:{uri:args[args.indexOf('--uri')+1]}})});const frame=await response.json();console.log(JSON.stringify(frame.result));`};
  for(const[name,source]of Object.entries(sources)){const path=join(packageRoot,name);mkdirSync(dirname(path),{recursive:true});writeFileSync(path,source);}
  selected.files=Object.fromEntries(Object.entries(sources).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([name,source])=>[name,sha(source)]));
  selected.integrity="explicit-offline-not-published";writeFileSync(manifestPath,JSON.stringify(manifest));
  return {directory,root,manifest,close:()=>rmSync(directory,{recursive:true,force:true})};
}

function rejudge(directory,probe,tenantId) {
  const program=`import sys,json\nsys.path.insert(0,sys.argv[1])\nimport mcp_map_package_validation as v\nx=json.load(sys.stdin)\nf,_=v.observed_wire(x['wire'],'resources/read',${JSON.stringify(params).replaceAll('"',"'")},True,tenant_id=x.get('tenantId'))\nv.client_observation(x['client'],f,x['wire'],x['client']['canonicalClient'],x['client']['version'])`;
  return spawnSync("python3",["-I","-B","-c",program,directory],{input:JSON.stringify({...probe,...(tenantId===undefined?{}:{tenantId})}),encoding:"utf8",timeout:10000});
}

for(const selector of ["sdk","inspector"])for(const tenantId of [undefined,tenant])test(`offline fixed ${selector} preserves ${tenantId===undefined?"legacy no-tenant shape":"explicit tenant on initialize and invoke"}`,async()=>{
  const server=await candidate(),control=installation();
  try{
    const {launchCanonicalClient}=await import(pathToFileURL(join(control.directory,"mcp_fixed_client_launcher.mjs")).href);
    const probe=await observeCandidateRequest({method:"resources/read",params,authenticated:true,credential:controlCredential,credentialSalt:salt,
      ...(tenantId===undefined?{}:{tenantId}),invoke:endpoint=>launchCanonicalClient({installationRoot:control.root,selector,endpoint,method:"resources/read",params,
        authenticated:true,credential:controlCredential,...(tenantId===undefined?{}:{tenantId})})});
    assert.equal(probe.client.exitCode,0,probe.client.stderr);assert.equal(rejudge(control.directory,probe,tenantId).status,0);
    assert.deepEqual(server.seen.map(value=>value.tenant),[tenantId,tenantId]);
    if(tenantId===undefined){
      assert.equal(Object.hasOwn(probe.wire,"expectedTenantId"),false);
      assert.equal(Object.hasOwn(probe.wire.initialize.request,"tenantId"),false);
      assert.equal(Object.hasOwn(probe.wire.invocation.request,"tenantId"),false);
      assert.equal(Object.hasOwn(probe.client.launch.request,"tenantId"),false);
      assert.deepEqual(Object.keys(probe.wire).sort(),["schema","upstream","method","expectedParams","authenticated","expectedCredentialFingerprint","listener","initialize","invocation","invalid","completed","clientProcessId"].sort());
    }else{
      assert.equal(probe.wire.expectedTenantId,tenant);assert.equal(probe.client.launch.request.tenantId,tenant);
      for(const frame of ["initialize","invocation"]){assert.equal(probe.wire[frame].request.tenantId,tenant);
        for(const mutation of [undefined,"different-tenant"]){const altered=structuredClone(probe);if(mutation===undefined)delete altered.wire[frame].request.tenantId;else altered.wire[frame].request.tenantId=mutation;
          assert.notEqual(rejudge(control.directory,altered,tenantId).status,0);}}
      const altered=structuredClone(probe);delete altered.client.launch.request.tenantId;assert.notEqual(rejudge(control.directory,altered,tenantId).status,0);
      assert.notEqual(rejudge(control.directory,probe,undefined).status,0);
    }
  }finally{control.close();await server.close();}
});

async function rawClient(endpoint,{initialHeaders={},invokeHeaders=initialHeaders,cleanupHeaders}) {
  const common={"content-type":"application/json","X-API-Key":controlCredential};
  const initial=await fetch(endpoint,{method:"POST",headers:{...common,...initialHeaders},body:JSON.stringify({jsonrpc:"2.0",id:1,method:"initialize",params:{}})});
  if(initial.status!==200)throw Error("offline initial refused");await initial.json();
  const session=initial.headers.get("mcp-session-id");
  const invoked=await fetch(endpoint,{method:"POST",headers:{...common,...invokeHeaders,"mcp-session-id":session},body:JSON.stringify({jsonrpc:"2.0",id:2,method:"resources/read",params})});
  if(invoked.status!==200)throw Error("offline invocation refused");await invoked.json();
  if(cleanupHeaders){const cleanup=await fetch(endpoint,{method:"DELETE",headers:{...common,...cleanupHeaders,"mcp-session-id":session}});await cleanup.text();}
  return {processId:process.pid};
}
for(const [name,expected,options]of [
 ["unrequested tenant",undefined,{initialHeaders:{"X-Honua-Tenant":tenant}}],
 ["missing tenant",tenant,{}],
 ["wrong invocation tenant",tenant,{initialHeaders:{"X-Honua-Tenant":tenant},invokeHeaders:{"X-Honua-Tenant":"other"}}],
 ["initial alias",undefined,{initialHeaders:{"X-Tenant-Id":tenant}}],
 ["invocation alias",tenant,{initialHeaders:{"X-Honua-Tenant":tenant},invokeHeaders:{"X-Honua-Tenant":tenant,"X-Honua-Other":tenant}}],
 ["cleanup alias",tenant,{initialHeaders:{"X-Honua-Tenant":tenant},cleanupHeaders:{"X-Honua-Tenant":tenant,"X-Tenant":tenant}}],
])test(`offline observer refuses ${name} without forwarding unauthorized context`,async()=>{
 const server=await candidate();let observed;
 try{await assert.rejects(observeCandidateRequest({method:"resources/read",params,authenticated:true,credential:controlCredential,credentialSalt:salt,
  ...(expected===undefined?{}:{tenantId:expected}),invoke:endpoint=>rawClient(endpoint,options),onObservation:value=>{observed=value;}}));
  assert.equal(observed.invalid,true);assert.ok(server.seen.every(value=>value.tenant===expected));
 }finally{await server.close();}
});

test("tenant options refuse anonymous, malformed, and arbitrary header values before invoking any client",async()=>{
 for(const value of ["", "bad/tenant", "bad\r\nX-API-Key: other", "*", null])await assert.rejects(observeCandidateRequest({method:"resources/read",params,
  authenticated:true,credential:controlCredential,credentialSalt:salt,tenantId:value,invoke:()=>{throw Error("must not invoke");}}),/tenant selection refused/);
 await assert.rejects(observeCandidateRequest({method:"resources/read",params,authenticated:false,credential:controlCredential,credentialSalt:salt,tenantId:tenant,invoke:()=>{throw Error("must not invoke");}}),/tenant selection refused/);
});
