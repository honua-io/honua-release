import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {randomUUID} from 'node:crypto';
import assert from 'node:assert/strict';
const consumer=process.env.HONUA_PARITY_CONSUMER,run=process.env.HONUA_PARITY_RUN,baseUrl='http://127.0.0.1:19816';
const publishedClients=JSON.parse(await readFile(`${consumer}/provenance.json`,'utf8'));
const fixtureProvenance=JSON.parse(await readFile(`${run}/published-map-envelope-provenance.json`,'utf8'));
const consumerRequire=createRequire(`${consumer}/package.json`);
const out=`${run}/studio-tenants-${new Date().toISOString().replaceAll(':','-')}`;await mkdir(out,{mode:0o700});
const load=(specifier)=>import(pathToFileURL(consumerRequire.resolve(specifier)).href);
const {HonuaClient}=await load('@honua/sdk-js/honua');
const {createHonuaCommandRuntime,studioDraftSaveVersionCommand}=await load('@honua/sdk-js/control-plane');
const {HonuaStudioLifecycleClient,createHonuaStudioCommandAdapter}=await load('@honua/sdk-js/studio');
const {createServer}=await load('@honua/mcp-server');
const require=createRequire(`${consumer}/node_modules/@honua/mcp-server/package.json`);
const {Client}=await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/index.js')));
const {InMemoryTransport}=await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/inMemory.js')));
const proof=JSON.parse(await readFile(`${run}/principal-proof.json`,'utf8'));
const token=async(user)=>JSON.parse(await readFile(`${run}/${user}-bearer.json`,'utf8')).accessToken;
const bundle=async(user)=>{const client=new HonuaClient({baseUrl,bearerToken:await token(user)});const p=proof.principals.find(p=>p.user===user);const identity={actor:p.claims.find(c=>c.type==='sub').value,tenantId:p.claims.find(c=>c.type==='tenant_id').value};return{client,identity,studio:new HonuaStudioLifecycleClient({client})};};
const a=await bundle('admin-a'),b=await bundle('admin-b');
const summary={publishedClients,fixtureProvenance,principals:{owner:a.identity,otherTenant:b.identity},scope:'Real tenant-owned Studio draft enforcement across shared SDK/Studio/MCP command adapters. No equivalent terminal draft verb exists; this is not four-surface journey certification.',results:[]};
const save=()=>writeFile(`${out}/summary.json`,JSON.stringify(summary,null,2)+'\n',{mode:0o600});
const normalize=(error)=>error?.toJSON?{...error.toJSON(),message:error.message}:{name:error.name,message:error.message,statusCode:error.statusCode,code:error.code};
try{
 const envelope=JSON.parse(await readFile(`${run}/published-map-envelope.json`,'utf8'));
 const draft=await a.studio.drafts.create({packageKey:`sdk1424-${randomUUID()}`,envelope});summary.draft=draft;await save();
 const owned=await a.studio.drafts.get(draft.draftId);assert.equal(owned.draftId,draft.draftId);
 const input={draftId:draft.draftId,generation:draft.generation};
 for(const [surface,call]of[
  ['sdk',()=>createHonuaCommandRuntime(b).execute(studioDraftSaveVersionCommand,input,{transport:'sdk'})],
  ['studio',()=>createHonuaStudioCommandAdapter(b).execute(studioDraftSaveVersionCommand,input)]
 ]){try{summary.results.push({surface,unexpectedSuccess:await call()});}catch(error){summary.results.push({surface,error:normalize(error)});}await save();}
 const server=createServer(b.client,{controlPlaneCommands:{runtime:createHonuaCommandRuntime(b),identity:b.identity}});
 const [ct,st]=InMemoryTransport.createLinkedPair(),mcp=new Client({name:'sdk1424-tenant-proof',version:'1.0.0'});
 await server.connect(st);await mcp.connect(ct);
 try{const result=await mcp.callTool({name:'honua_command_studio_draft_saveVersion',arguments:{input}});summary.results.push({surface:'mcp',isError:result.isError,error:JSON.parse(result.content.find(c=>c.type==='text').text)});}finally{await mcp.close();await server.close();}
 await save();
 const correlations=[...new Set(summary.results.map(r=>r.error?.correlationId).filter(Boolean))];
 summary.audit=[];for(const correlationId of correlations){const response=await fetch(`${baseUrl}/api/v1/admin/observability/audit?correlationId=${encodeURIComponent(correlationId)}&pageSize=100`,{headers:{Authorization:`Bearer ${await token('admin-a')}`}});summary.audit.push({correlationId,status:response.status,body:await response.json()});}await save();
 assert(summary.results.every(r=>r.error&&[403,404].includes(r.error.statusCode)),'Cross-tenant command succeeded or returned unexpected refusal');
 assert.equal(new Set(summary.results.map(r=>r.error.statusCode)).size,1);
 // A same-tenant authenticated owner remains able to access the object after every denied write.
 const after=await a.studio.drafts.get(draft.draftId);assert.equal(after.generation,draft.generation);summary.ownerAfter={draftId:after.draftId,generation:after.generation};
 summary.status='bounded-cross-tenant-passed';await save();console.log(JSON.stringify({status:summary.status,draftId:draft.draftId,results:summary.results,evidence:out}));
}catch(error){summary.status='failed';summary.error=normalize(error);await save();throw error;}
