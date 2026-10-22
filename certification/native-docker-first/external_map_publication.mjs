// Read-only capture of an ALREADY pending real human Console proposal.
// Never creates, reviews, submits, retries, approves or publishes anything.
import {createRequire} from 'node:module';
import {readFileSync,openSync,writeFileSync,closeSync} from 'node:fs';
import {resolve} from 'node:path';
import {createHash} from 'node:crypto';
const [consoleRoot,origin,humanBrowserCdp,inputPath,outputPath,route,proposalId]=process.argv.slice(2);
if (!consoleRoot || !/^http:\/\/127\.0\.0\.1:\d+$/.test(origin || '') || !/^\/[a-z0-9][a-z0-9/_-]{0,126}$/.test(route || '')) throw Error('Exact installed origin and scoped route required.');
const cdpPort=/^http:\/\/127\.0\.0\.1:([0-9]{1,5})$/.exec(humanBrowserCdp || '')?.[1];
if (!cdpPort || Number(cdpPort)<1 || Number(cdpPort)>65535) throw Error('Explicit loopback human browser CDP origin required.');
if (!/^[A-Za-z0-9_-]{1,128}$/.test(proposalId || '')) throw Error('Exact already-pending proposal ID required.');
const {chromium}=createRequire(resolve(consoleRoot,'e2e/playwright/package.json'))('playwright');
const map=JSON.parse(readFileSync(inputPath,'utf8'));
// Attach only. No launch, new context/page, navigation, storage injection,
// request interception or close command may alter the real human browser.
const browser=await chromium.connectOverCDP(humanBrowserCdp);
const check=(condition,message)=>{if(!condition)throw Error(message);};
let exitStatus=0;
try {
  const candidates=[];
  for(const context of browser.contexts())for(const page of context.pages()) {
    let location;try{location=new URL(page.url());}catch{continue;}
    if(location.origin!==origin || location.pathname!=='/studio')continue;
    const matches=await page.evaluate(expected=>{
      const app=document.querySelector('honua-studio-app');
      const workspace=window.honuaAgentWorkspace;
      return app?.workspaceDraftIdentity?.draftId===expected.reopenedDraftId &&
        workspace?.workspaceId===expected.workspaceId && workspace.viewId===expected.viewId &&
        workspace.context?.task?.id===expected.reopenedDraftId &&
        workspace.context?.targetId===expected.targetId && workspace.context?.tenantId===expected.tenantId;
    },map);
    if(matches)candidates.push(page);
  }
  check(candidates.length===1,'Exactly one already-open original human Studio tab with matching workspace/view/map is required.');
  const page=candidates[0];
  const sessionResponse=await page.request.get(origin+'/api/agent-workspace/session');
  check(sessionResponse.status()===200,'Original real human session unavailable.');
  const session=await sessionResponse.json();check(session.authenticated && session.principalId && session.environmentId,'Authenticated human identity required.');
  const headers={'X-Honua-Environment':session.environmentId,'X-Honua-Session-Partition':session.principalId};
  const read=async path=>{const r=await page.request.get(origin+path,{headers});check(r.status()===200,'Canonical human read unavailable.');const v=await r.json();return v.data??v;};
  const host=page.locator('[data-agent-workspace]');
  // The existing human tab must retain this workspace and map throughout
  // observation. We never connect another workspace or change the selected task.
  await page.waitForFunction(expected=>{
    const app=document.querySelector('honua-studio-app');
    const workspace=window.honuaAgentWorkspace;
    return app?.workspaceDraftIdentity?.draftId===expected.reopenedDraftId &&
      workspace?.workspaceId===expected.workspaceId && workspace.context?.task?.id===expected.reopenedDraftId;
  },map);
  check(await host.getByRole('log',{includeHidden:true}).textContent().then(value=>value.includes('The map draft is ready for your review. Publication still needs your action.')),'Same Console conversation handoff unavailable.');
  const draft=await read(`/api/studio/v1/studio/package-drafts/${encodeURIComponent(map.reopenedDraftId)}`);
  const version=await read(`/api/studio/v1/studio/content-items/${encodeURIComponent(map.itemId)}/versions/${encodeURIComponent(map.versionId)}`);
  check(draft.itemId===map.itemId && version.contentHash===map.contentHash && JSON.stringify(draft.envelope.body)===JSON.stringify(version.envelope.body),'Human observes a different canonical draft/version.');
  const layers=draft.envelope.body.layers.filter(layer=>layer.sourceId===map.studioSourceId);
  check(layers.length===1 && layers[0].id===map.layerId,'Original imported service source absent from human map.');
  await page.waitForFunction(id=>{
    const map=document.querySelector('honua-studio-canvas')?.mapView?.map;
    return map?.getLayer(id) && map.queryRenderedFeatures({layers:[id]}).length>0;
  },map.layerId,{timeout:45_000});
  // Independent live paint, not just a stored style or nonempty canvas.
  const liveBlue=await page.locator('honua-studio-canvas').evaluate((node,id)=>{
    const live=node.mapView?.map;
    return ['fill-color','line-color','circle-color','text-color'].some(property=>{
      try{return live?.getPaintProperty(id,property)==='#2563eb';}catch{return false;}
    });
  },map.layerId);
  check(liveBlue,'Live rendered presentation layer does not have the observed blue refinement.');
  const proposal=await read(`/api/processes/admin/proposals/${encodeURIComponent(proposalId)}`);
  check(proposal.proposalId===proposalId && proposal.status?.toLowerCase()==='awaitingapproval' && typeof proposal.requestedBy==='string' && proposal.requestedBy.trim() && !proposal.requestedByAgent && !proposal.resolvedBy,'Exact unresolved human proposal unavailable; direct publication is refused.');
  const records=await page.locator('honua-studio-app').evaluate(node=>{
    const storage=node.workspace?.storage, values=[];
    if(storage)for(let i=0;i<storage.length;i++){const key=storage.key(i);if(key?.startsWith('honua.studio.publication.'))values.push(JSON.parse(storage.getItem(key)));}
    return values;
  });
  const matching=records.filter(value=>value.itemId===map.itemId && value.versionId===map.versionId && value.receipt?.proposalId===proposalId && value.receipt?.status==='awaiting-approval' && value.contentHash===map.contentHash && value.input?.intent?.route===route && value.input?.intent?.visibility==='public');
  check(matching.length===1 && matching[0].receipt.operationInstanceId,'Exact durable pending publication operation unavailable.');
  const handoff={schemaVersion:'honua.native-docker.publication-handoff/v1',status:'awaiting-separate-human-approval',
    ...map,principalId:session.principalId,environmentId:session.environmentId,versionNumber:version.versionNumber,
    audience:'public',route,proposalId,requestedBy:proposal.requestedBy,commandId:matching[0].commandId,
    operationInstanceId:matching[0].receipt.operationInstanceId,requestId:matching[0].receipt.requestId,
    sourceId:map.studioSourceId,renderedMapVerified:true,
    canonicalProposal:{proposalId:proposal.proposalId,status:proposal.status,requestedBy:proposal.requestedBy,
      requestedByAgent:proposal.requestedByAgent,resolvedBy:proposal.resolvedBy,diff:proposal.diff},
    bodySha256:createHash('sha256').update(JSON.stringify(version.envelope.body)).digest('hex')};
  // Capture is create-only. A failed observation may be repeated without any
  // publication action; the controller independently joins the actual handle.
  const fd=openSync(outputPath,'wx',0o600);try{writeFileSync(fd,JSON.stringify(handoff,null,2)+'\n');}finally{closeSync(fd);}
} catch(error) {
  console.error('Read-only human proposal capture blocked: '+error.message);
  exitStatus=1;
} finally {
  // Playwright has no public disconnect-only method. Ending this dedicated
  // observer process closes its CDP transport without Browser.close,
  // context.close, navigation or any command affecting the human's session.
  process.exit(exitStatus);
}
