import json,os,socket,urllib.parse
from pathlib import Path
from html.parser import HTMLParser
import requests
from principal_claims import check_principal
p=Path(__file__).resolve().parent
os.umask(0o077)
original=socket.getaddrinfo
def resolve(host,*args,**kwargs):
    return original('127.0.0.1' if host=='host.docker.internal' else host,*args,**kwargs)
socket.getaddrinfo=resolve
base='http://127.0.0.1:19816'
class Form(HTMLParser):
    def __init__(self):super().__init__();self.action=None;self.fields={}
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form' and a.get('id')=='kc-form-login':self.action=a['action']
        if tag=='input' and a.get('type')=='hidden' and a.get('name'):self.fields[a['name']]=a.get('value','')
def checked(r,expected=200):
    if r.status_code!=expected:
        raise RuntimeError(f'{r.request.method} {urllib.parse.urlsplit(r.url).path} returned {r.status_code}; body saved privately')
    return r
results=[]
for user,password in json.loads((p/'credentials.json').read_text()).items():
    s=requests.Session();s.trust_env=False;s.verify=str(p/'certs/kc.crt')
    config=checked(s.get(base+'/api/v1/admin/auth/config',timeout=30)).json()
    provider=config['providers'][0]['key']
    auth=checked(s.post(base+f'/api/v1/admin/auth/providers/{provider}/authorize-url',json={},timeout=30))
    page=checked(s.get(auth.json()['authorizeUrl'],timeout=30))
    parser=Form();parser.feed(page.text)
    if not parser.action:
        (p/f'{user}-login-error.html').write_text(page.text)
        raise RuntimeError('No genuine Keycloak login form')
    login=s.post(parser.action,data={**parser.fields,'username':user,'password':password,'credentialId':''},allow_redirects=False,timeout=30)
    if login.status_code!=302:
        (p/f'{user}-login-error.html').write_text(login.text)
        raise RuntimeError(f'{user} login returned {login.status_code}')
    callback=urllib.parse.urlsplit(login.headers['Location'])
    assert callback.scheme+'://'+callback.netloc==base and callback.path=='/admin/auth/callback'
    query=urllib.parse.parse_qs(callback.query)
    token=s.post(base+f'/api/v1/admin/auth/providers/{provider}/token',json={'grantType':'authorization_code','code':query['code'][0],'state':query['state'][0]},timeout=30)
    if token.status_code not in (200,204):(p/f'{user}-exchange-error.json').write_text(token.text)
    checked(token,204)
    session=checked(s.get(base+'/api/v1/admin/auth/session',timeout=30)).json()
    assert session['isAuthenticated'] is True
    bearer=checked(s.post(base+'/api/v1/admin/auth/bearer',json={},timeout=30)).json()
    (p/f'{user}-bearer.json').write_text(json.dumps(bearer,indent=2)+'\n')
    (p/f'{user}-cookies.json').write_text(json.dumps(requests.utils.dict_from_cookiejar(s.cookies),indent=2)+'\n')
    client=requests.Session();client.trust_env=False
    headers={'Authorization':'Bearer '+bearer['accessToken']}
    response=client.get(base+'/api/v1/admin/connections',headers=headers,timeout=30)
    cross=client.get(base+'/api/v1/admin/connections',headers={**headers,'X-Honua-Tenant':'tenant-b' if user.endswith('-a') else 'tenant-a'},timeout=30)
    claims=[c for c in session['claims'] if c['type'] in ('scope','scp','honua:oauth_scope_governed','tenant_id','roles','role','sub','preferred_username','http://schemas.microsoft.com/ws/2008/06/identity/claims/role')]
    result={'user':user,'provider':provider,'claims':claims,'bearerExpiresAt':bearer['expiresAt'],'connectionsStatus':response.status_code,'crossTenantOverrideStatus':cross.status_code}
    results.append(result)
    (p/'principal-proof.json').write_text(json.dumps({'authentication':'real Keycloak authorization code + PKCE; server session-derived operator bearer','principals':results},indent=2)+'\n')
    check_principal(user,claims,response.status_code,cross.status_code)
    assert len({next(c['value'] for c in item['claims'] if c['type']=='sub') for item in results})==len(results),'Principals unexpectedly share a subject'
    print(json.dumps(result),flush=True)
(p/'principal-proof.json').write_text(json.dumps({'authentication':'real Keycloak authorization code + PKCE; server session-derived operator bearer','principals':results},indent=2)+'\n')
