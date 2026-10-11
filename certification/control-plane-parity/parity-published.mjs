import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { randomUUID, createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import assert from 'node:assert/strict';
const consumer=process.env.HONUA_PARITY_CONSUMER;
const root=`${consumer}/node_modules/@honua/sdk-js`;
const publishedClients=JSON.parse(await readFile(`${consumer}/provenance.json`,'utf8'));
const fixtureProvenance=JSON.parse(await readFile(`${process.env.HONUA_PARITY_RUN}/published-map-envelope-provenance.json`,'utf8'));
const consumerRequire=createRequire(`${consumer}/package.json`);
const run=process.env.HONUA_PARITY_RUN;
const out=process.env.PARITY_OUTPUT_DIR??`${run}/parity-${new Date().toISOString().replaceAll(':','-')}`;
await mkdir(out,{mode:0o700});
const load=(specifier)=>import(pathToFileURL(consumerRequire.resolve(specifier)).href);
const {HonuaClient}=await load('@honua/sdk-js/honua');
const {createHonuaCommandRuntime,connectionTestCommand,mapPackagePublishCommand,importCreateCommand}=await load('@honua/sdk-js/control-plane');
const {createHonuaStudioCommandAdapter}=await load('@honua/sdk-js/studio');
const {createServer}=await load('@honua/mcp-server');
const require=createRequire(`${consumer}/node_modules/@honua/mcp-server/package.json`);
const {Client}=await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/client/index.js')));
const {InMemoryTransport}=await import(pathToFileURL(require.resolve('@modelcontextprotocol/sdk/inMemory.js')));
const baseUrl='http://127.0.0.1:19816';
const publish=process.env.PARITY_MODE==='publish';
const importing=process.env.PARITY_MODE==='import';
const successfulConnection=process.env.PARITY_MODE==='connection';
const command=publish?mapPackagePublishCommand:importing?importCreateCommand:connectionTestCommand;
const proof=JSON.parse(await readFile(`${run}/principal-proof.json`,'utf8'));
const bearer=async(user)=>JSON.parse(await readFile(`${run}/${user}-bearer.json`,'utf8')).accessToken;
const tokenA=await bearer('admin-a');
const importUrl=(await readFile(`${run}/owned-import-url.txt`,'utf8')).trim();
const save=async(name,data)=>writeFile(`${out}/${name}.json`,JSON.stringify(data,null,2).replaceAll(importUrl,'<private-presigned-import-source>').replace(/https?:[^\s\"<>]*X-Amz-[^\s\"<>]*/gi,'<private-presigned-import-source>')+'\n',{mode:0o600});
const normalize=(e)=>e?.toJSON ? {...e.toJSON(),message:e.message} : e;
const summary={publishedClients,fixtureProvenance,server:JSON.parse(await readFile(`${run}/runtime-proof.json`,'utf8')),commandId:command.id,scope:'Real MCP protocol roundtrip, actual CLI executable, Studio adapter and direct SDK for the declared command. Persisted actor/tenant audit and negative-control diagnostics; each run is one bounded journey stage, not full release certification.',cases:[]};
async function invokeAll(user,connectionId){
 const token=await bearer(user),client=new HonuaClient({baseUrl,bearerToken:token});
 const p=proof.principals.find(p=>p.user===user);
 const identity={actor:p.claims.find(c=>c.type==='sub').value,tenantId:p.claims.find(c=>c.type==='tenant_id').value};
 const results=[];
 let input={connectionId},cliArgs=['connection','test',connectionId];
 if(publish){
  const envelope=JSON.parse(await readFile(`${run}/published-map-envelope.json`,'utf8'));
  const fixture=JSON.parse(await readFile(`${run}/imported-feature-collection.json`,'utf8'));
  const importProvenance=JSON.parse(await readFile(`${run}/imported-feature-provenance.json`,'utf8'));
  summary.importProvenance=importProvenance;
  const ownedMap={version:8,sources:{fixture:{type:'geojson',data:fixture}},layers:[{id:'fixture-points',type:'circle',source:'fixture'}]};
  const pkg={...envelope.body,sourceBindings:[],mapSpec:ownedMap,mapPackageId:`sdk1424-${connectionId}`,createdAt:new Date().toISOString()};
  input={mapId:`sdk1424-${connectionId}`,package:pkg};
  summary.packageInputs??={};summary.packageInputs[user]={mapId:input.mapId,requestPackageSha256:createHash('sha256').update(JSON.stringify(pkg)).digest('hex'),readbackRecordSha256:importProvenance.readbackRecordSha256};
  const packagePath=`${out}/${user}-map-package.json`;await writeFile(packagePath,JSON.stringify(pkg),{mode:0o600});
  cliArgs=['map','publish',input.mapId,'--package',`@${packagePath}`];
 }
 if(importing){
  const sourceUrl=importUrl;
  input={sourceKind:'geojson',sourceUrl,options:{fileName:'fixture.geojson',tableName:`sdk1424_${connectionId.replaceAll('-','')}`,targetSchema:'public'}};
  const optionsPath=`${out}/${user}-import-options.json`;await writeFile(optionsPath,JSON.stringify(input.options),{mode:0o600});
  cliArgs=['import','create','--source-kind',input.sourceKind,'--source-url',input.sourceUrl,'--options',`@${optionsPath}`];
 }
 const env={...process.env,HONUA_BASE_URL:baseUrl,HONUA_BEARER_TOKEN:token,HONUA_CONFIG_HOME:`${out}/empty-config`};
 delete env.HONUA_API_KEY;delete env.HONUA_ADMIN_KEY;
 const cli=spawnSync(process.execPath,[`${root}/dist/src/cli/bin.js`,...cliArgs,'--yes','--json','--actor',identity.actor,'--tenant',identity.tenantId],{env,encoding:'utf8',timeout:30000,maxBuffer:1024*1024});
 for(const output of [cli.stdout,cli.stderr])assert(!output.includes(token),'CLI leaked bearer');
 const cliOutput=JSON.parse(cli.stdout.trim()||cli.stderr.trim());
 results.push({surface:'cli',exitCode:cli.status,value:cliOutput});
 const server=createServer(client,{controlPlaneCommands:{runtime:createHonuaCommandRuntime({client}),identity}});
 const [ct,st]=InMemoryTransport.createLinkedPair();
 const mcp=new Client({name:'honua-1424-live-parity',version:'1.0.0'});
 await server.connect(st);await mcp.connect(ct);
 try{
  const response=await mcp.callTool({name:`honua_command_${command.id.replace(/[.-]/g,'_')}`,arguments:{input}});
  results.push({surface:'mcp',isError:response.isError??false,value:JSON.parse(response.content.find(c=>c.type==='text').text)});
 }finally{await mcp.close();await server.close();}
 for(const [surface,call] of [
  ['studio',()=>createHonuaStudioCommandAdapter({client,identity}).execute(command,input)],
  ['sdk',()=>createHonuaCommandRuntime({client,identity}).execute(command,input,{transport:'sdk'})]
 ]){try{results.push({surface,value:await call()});}catch(e){results.push({surface,value:normalize(e)});}}
 await save(`${user}-responses`,results);
 assert.equal(new Set(results.map(r=>r.value.correlationId)).size,1,'correlation differs');
 assert.equal(new Set(results.map(r=>r.value.errorKind??r.value.status)).size,1,'classification differs');
 assert.equal(new Set(results.map(r=>r.value.statusCode)).size,1,'HTTP status differs');
 const correlationId=results[0].value.correlationId;assert(correlationId);
 const auditResponse=await fetch(`${baseUrl}/api/v1/admin/observability/audit?correlationId=${encodeURIComponent(correlationId)}&pageSize=100`,{headers:{Authorization:`Bearer ${tokenA}`}});
 assert.equal(auditResponse.status,200);const audit=await auditResponse.json();await save(`${user}-audit-api`,audit);
 const safeCorrelation=correlationId.replaceAll("'","''");
 const query=`select coalesce(json_agg(t),'[]'::json) from (select audit_id,actor,action,outcome,correlation_id,resource_type,resource_id,details,entry_hash from honua.audit_log where correlation_id='${safeCorrelation}' order by audit_id) t`;
 const sql=spawnSync('docker',['exec','honua-sdk1424-fixed-db-1','psql','-U','honua','-d','honua','-Atc',query],{encoding:'utf8'});assert.equal(sql.status,0);const persisted=JSON.parse(sql.stdout);await save(`${user}-audit-postgres`,persisted);
 const item={user,identity,connectionId,correlationId,status:results[0].value.statusCode,errorKind:results[0].value.errorKind,auditApiRows:audit.items.length,persistedRows:persisted.length,results};
 if(user==='admin-a'){
  if(publish){
   assert(results.every(r=>r.value.output?.proposalId),'missing governed proposal');
   for(const key of ['itemId','versionId','contentHash','proposalId','operationInstanceId','auditId','correlationId']) assert.equal(new Set(results.map(r=>r.value.output[key])).size,1,`server ${key} differs`);
   const output=results[0].value.output;assert.equal(output.publicationStatus,'AwaitingApproval');
   item.governedJoin=Object.fromEntries(['itemId','versionId','contentHash','proposalId','operationInstanceId','auditId','correlationId'].map(key=>[key,output[key]]));
  }else if(importing){
   assert(results.every(r=>r.value.output?.id),'missing import job id');
   assert.equal(new Set(results.map(r=>r.value.output.id)).size,1,'idempotent import job differs');
   const jobId=results[0].value.output.id;item.importJobId=jobId;
   const accepted=persisted.filter(row=>row.action==='operation.accepted');
   assert.equal(accepted.length,1,'expected one durable canonical acceptance across adapters');
   assert(persisted.filter(row=>row.action==='operation.retry').length>=3,'missing durable canonical retry audits');
   const operationResponse=await fetch(`${baseUrl}/api/v1/operations/handles/${encodeURIComponent(accepted[0].resource_id)}`,{headers:{Authorization:`Bearer ${token}`}});
   assert.equal(operationResponse.status,200);const operation=await operationResponse.json();await save(`${user}-operation-handle`,operation);
   assert.equal(operation.data.jobId,jobId,'operation does not join original import job');
   assert.equal(operation.data.auditId,String(accepted[0].audit_id),'operation does not join accepted durable audit');
   item.governedJoin={jobId,operationInstanceId:operation.data.operationInstanceId,auditId:operation.data.auditId,correlationId:operation.data.correlationId};

   let job;for(let attempt=0;attempt<30;attempt++){
    const response=await fetch(`${baseUrl}/api/v1/admin/import/jobs/${encodeURIComponent(jobId)}`,{headers:{Authorization:`Bearer ${token}`}});
    assert.equal(response.status,200);job=await response.json();await save(`${user}-import-status`,job);
    const raw=(job.data??job).status;const state=typeof raw==='number'?['queued','processing','completed','failed','cancelled'][raw]:raw?.toLowerCase();if(['completed','succeeded','failed','cancelled'].includes(state))break;
    await new Promise(resolve=>setTimeout(resolve,1000));
   }
   item.importJob=job;assert((job.data??job).status===2||['completed','succeeded'].includes(String((job.data??job).status).toLowerCase()),'import failed or did not complete');
  }else if(successfulConnection){assert(results.every(r=>r.value.output?.isHealthy===true || r.value.output?.data?.isHealthy===true),'connection unhealthy');}else assert.equal(item.status,404);
  assert(persisted.length>=4,'missing durable audit rows');
  assert.equal(new Set(persisted.filter(r=>r.action==='admin.post').map(r=>r.actor)).size,1);
  assert(persisted.filter(r=>r.action==='admin.post').every(r=>r.actor.includes(identity.actor)));
  assert(persisted.filter(r=>r.action==='admin.post').every(r=>JSON.parse(r.details).tenantId===identity.tenantId));
 }else assert.equal(item.status,403);
 summary.cases.push(item);await save('summary',summary);
 console.log(JSON.stringify({user,status:item.status,errorKind:item.errorKind,correlationId,auditApiRows:item.auditApiRows,persistedRows:item.persistedRows}));
}
const id=successfulConnection?JSON.parse(await readFile(`${run}/connection-setup.json`,'utf8')).data.connectionId:randomUUID();
summary.failures=[];
for(const user of ['admin-a','viewer-a',...(publish||importing?['admin-read-a']:[])]){
 try{await invokeAll(user,user==='admin-read-a'?randomUUID():id);}
 catch(error){summary.failures.push({user,error:String(error)});await save('summary',summary);console.log(JSON.stringify({user,failure:String(error)}));}
}
summary.status=summary.failures.length?'failed':'bounded-parity-passed';
await save('summary',summary);console.log(`Evidence: ${out}`);
if(summary.failures.length)process.exitCode=1;
