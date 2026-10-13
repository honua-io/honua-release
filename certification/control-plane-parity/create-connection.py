from pathlib import Path
import json,uuid,requests,os
os.umask(0o077);run=Path(__file__).resolve().parent
values=dict(line.split('=',1) for line in (run/'run.env').read_text().splitlines() if '=' in line and not line.startswith('#'))
token=json.loads((run/'admin-a-bearer.json').read_text())['accessToken']
request={'name':'sdk1424-fixed-'+uuid.uuid4().hex[:12],'host':'db','port':5432,'databaseName':'honua','username':'honua','password':values['S4_DB_PASSWORD'],'provider':'postgis','sslRequired':False,'sslMode':'Disable'}
r=requests.post('http://127.0.0.1:19816/api/v1/admin/connections',headers={'Authorization':'Bearer '+token},json=request,timeout=45)
assert token not in r.text and values['S4_DB_PASSWORD'] not in r.text,'Credential leaked in connection response'
(run/'connection-setup.json').write_text(json.dumps(r.json(),indent=2)+'\n')
assert r.status_code==201,f'Connection creation returned {r.status_code}; see private response'
print(json.dumps({'status':r.status_code,'connectionId':r.json()['data']['connectionId'],'purpose':'fresh fixture identity prevents replaying copied operation records'}))
