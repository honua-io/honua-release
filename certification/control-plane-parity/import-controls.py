from pathlib import Path
from response_receipt import response_body
from concurrent.futures import ThreadPoolExecutor
import requests,json,uuid,time,os,atexit,subprocess,re
os.umask(0o077)
run=Path(__file__).resolve().parent;base='http://127.0.0.1:19816'
source=(run/'owned-import-url.txt').read_text().strip()
tokens={u:json.loads((run/f'{u}-bearer.json').read_text())['accessToken'] for u in ['admin-a','admin-b','viewer-a','admin-read-a']}
key='sdk1424-control-'+uuid.uuid4().hex; table='sdk1424_'+uuid.uuid4().hex[:10]
body={'sourceUrl':source,'fileName':'fixture.geojson','tableName':table,'targetSchema':'public','forceBackground':True}
proof={'idempotencyKey':key,'sourceSha256':json.loads((run/'owned-import-object.json').read_text())['sha256'],'requests':[],'jobs':{},'status':'incomplete'}
def retain():
 text=json.dumps(proof,indent=2).replace(source,'<private-presigned-import-source>')+'\n'
 for token in tokens.values():assert token not in text
 (run/'import-controls.json').write_text(text)
atexit.register(retain)
def sql(query):return subprocess.check_output(['docker','exec','honua-sdk1424-fixed-db-1','psql','-U','honua','-d','honua','-Atc',query],text=True).strip()
def tables():return set(sql("select table_name from information_schema.tables where table_schema='public' and table_name like 'imported_sdk1424%'").splitlines())
before=tables()
def send(user,payload=body,path='/api/v1/admin/import/upload-url'):
 r=requests.post(base+path,headers={'Authorization':'Bearer '+tokens[user],'Idempotency-Key':key,'X-Correlation-ID':key},json=payload,timeout=90)
 result={'user':user,'path':path,'httpStatus':r.status_code,'body':response_body(r),'operationInstanceId':r.headers.get('X-Honua-Operation-Instance-Id'),'auditId':r.headers.get('X-Honua-Audit-Id')}
 proof['requests'].append(result);return result
with ThreadPoolExecutor(max_workers=4) as pool: initial=list(pool.map(lambda _:send('admin-a'),range(4)))
assert all(x['httpStatus'] in [202,409] for x in initial), 'Concurrent request returned unexpected status'
accepted=[x for x in initial if x['httpStatus']==202];assert accepted,'No invocation owner accepted'
job=accepted[0]['body']['jobId'];assert all(x['body']['jobId']==job for x in accepted),'More than one concurrent job'
retry=send('admin-a',path='/api/v1/admin/imports');assert retry['httpStatus']==202 and retry['body']['jobId']==job
conflict=send('admin-a',{**body,'tableName':table+'_changed'});assert conflict['httpStatus']==409 and 'jobId' not in conflict['body']
for user in ['viewer-a','admin-read-a']:
 denied=send(user,{**body,'tableName':table+'_denied'});assert denied['httpStatus']==403 and 'jobId' not in denied['body']
other=send('admin-b',{**body,'tableName':table+'_tenant_b'});assert other['httpStatus']==202 and other['body']['jobId']!=job
assert other['operationInstanceId']!=retry['operationInstanceId'],'Tenant/actor reused another principal operation'
foreign=requests.get(base+'/api/v1/operations/handles/'+retry['operationInstanceId'],headers={'Authorization':'Bearer '+tokens['admin-b']},timeout=20)
proof['crossTenantOperationRead']={'status':foreign.status_code,'body':response_body(foreign)};assert foreign.status_code==404,'Other tenant could read original operation'
for user,jobId in [('admin-a',job),('admin-b',other['body']['jobId'])]:
 for _ in range(90):
  r=requests.get(base+'/api/v1/admin/import/jobs/'+jobId,headers={'Authorization':'Bearer '+tokens[user]},timeout=20);assert r.status_code==200
  status=r.json();state=(status.get('data') or status).get('status')
  if state in [2,3,4,'completed','succeeded','failed','cancelled']:break
  time.sleep(1)
 assert state in [2,'completed','succeeded'], 'Owned background job did not complete'
 proof['jobs'][user]={'jobId':jobId,'status':status}
completed=send('admin-a',path='/api/v1/admin/imports');assert completed['httpStatus']==202 and completed['body']['jobId']==job
added=tables()-before;assert len(added)==2,'Positive and cross-tenant principals must create only two tables; denied calls create none'
proof['rowCounts']={}
for name in added:
 assert re.fullmatch(r'[a-z0-9_]+',name)
 count=int(sql(f'SELECT count(*) FROM public."{name}"'));assert count==1,'Concurrent owner imported duplicate rows'
 proof['rowCounts'][name]=count
proof['status']='passed';proof['inflightRetryContract']='HTTP409 permits retry later; an accepted response must name the single original job'
text=json.dumps(proof,indent=2).replace(source,'<private-presigned-import-source>')+'\n'
for token in tokens.values():assert token not in text
(run/'import-controls.json').write_text(text)
print(json.dumps({'status':proof['status'],'concurrentStatuses':[x['httpStatus'] for x in initial],'originalJob':job,'otherTenantJob':other['body']['jobId']}))
