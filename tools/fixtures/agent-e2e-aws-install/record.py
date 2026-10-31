"""Record the shipped MCP process with a local substrate document fixture, never AWS."""
import subprocess,json,os,select,hashlib,pathlib,datetime,base64
import argparse
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--workdir', type=pathlib.Path, required=True)
parser.add_argument('--devops-repo', required=True)
parser.add_argument('--binary', type=pathlib.Path, required=True)
parser.add_argument('--output', type=pathlib.Path, required=True)
args=parser.parse_args()
BASE=args.workdir.resolve()
BASE.mkdir(parents=True,exist_ok=False)
OUT=args.output.resolve()
OUT.mkdir(parents=True,exist_ok=True)
REPO=args.devops_repo
BINARY=str(args.binary.resolve())
def source(path): return subprocess.check_output(['git','-C',REPO,'show','v2026.1.2:'+path])
root=BASE/'substrate'; aws=root/'infrastructure/terraform/examples/aws'; aws.mkdir(parents=True,exist_ok=True)
for name in ['main.tf','variables.tf','.terraform.lock.hcl','terraform.tfvars']:(aws/name).write_text('# recording fixture\n')
contracts=root/'infrastructure/terraform/contracts';contracts.mkdir(parents=True,exist_ok=True)
for name in ['operator-contract.v2.schema.json','terraform-exact-plan.v1.schema.json','terraform-exec-receipt.v1.schema.json']:
 (contracts/name).write_bytes(source('tests/Honua.DevOps.Agent.Tests/fixtures/honua-iac/contracts/'+name))
(OUT/'approval.schema.json').write_bytes(source('contracts/honua-devops-provision-approval.v1.schema.json'))
meta=json.loads(source('tests/Honua.DevOps.Agent.Tests/fixtures/honua-iac/documents/exact-plan-metadata.json'))
(root/'metadata.json').write_text(json.dumps(meta))
contract={'schema_version':'v1','kind':'honua.iac.deploy-identity','role_arn':'arn:aws:iam::123456789012:role/honua-deploy-dev','topology':{'compute':'ecs','redis':False,'custom_domain':True,'waf':False,'gp_batch':False},'actions_by_service':{'ecs':['ecs:CreateService','ecs:DescribeServices']}}
contract_path=root/'deploy-identity-contract.json';contract_path.write_text(json.dumps(contract,separators=(',',':')))
(OUT/'deploy-identity-contract.json').write_bytes(contract_path.read_bytes())
binpath=root/'bin';binpath.mkdir(exist_ok=True)
(binpath/'aws').write_text('#!/usr/bin/env python3\nimport json,sys\nassert sys.argv[1:3]==["sts","get-caller-identity"]\nprint(json.dumps({"Arn":"arn:aws:sts::123456789012:assumed-role/honua-deploy-dev/session","Account":"123456789012"}))\n')
(binpath/'terraform').write_text('#!/usr/bin/env python3\nimport sys\nassert sys.argv[1]=="show"\nprint("  # aws_ecs_service.honua will be created\\n  + resource \\\"aws_ecs_service\\\" \\\"honua\\\" {}\\nPlan: 1 to add, 0 to change, 0 to destroy.")\n')
for p in binpath.iterdir():p.chmod(0o755)
scripts=root/'scripts';scripts.mkdir(exist_ok=True)
(scripts/'terraform-exact-plan.sh').write_text('''#!/usr/bin/env bash
exec python3 - "$@" <<'DOC'
import sys,json,pathlib,hashlib,datetime
args=sys.argv[1:];opt=lambda n:args[args.index(n)+1]
root=pathlib.Path.cwd();m=json.loads((root/'metadata.json').read_text())
p=pathlib.Path(opt('--plan-out'));p.write_text('fake saved terraform plan')
m['actor_id']=opt('--actor');m['target_id']=opt('--target-id');m['action']=opt('--action')
m['source']['terraform_root']='infrastructure/terraform/examples/aws'
m['created_at_utc']=datetime.datetime.now(datetime.timezone.utc).isoformat();m['expires_at_utc']=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=30)).isoformat()
m['plan']['sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
del m['plan_metadata_digest'];m['plan_metadata_digest']=hashlib.sha256(json.dumps(m,sort_keys=True,separators=(',',':')).encode()).hexdigest()
pathlib.Path(opt('--metadata-out')).write_text(json.dumps(m))
print('Plan: 1 to add, 0 to change, 0 to destroy.')
DOC
''')
for name in ['terraform-exact-apply.sh','terraform-backend-identity.sh']:(scripts/name).write_text('#!/usr/bin/env bash\nexit 91\n')
env={'PATH':str(binpath)+':'+os.environ['PATH'],'HOME':str(BASE),'XDG_DATA_HOME':str(BASE/'data'),'TMPDIR':str(BASE/'tmp'),'HONUA_DEVOPS_ALLOWED_ENVIRONMENTS':'dev','HONUA_DEVOPS_TERRAFORM_LOCAL_PATH':str(root),'HONUA_DEVOPS_DEPLOY_IDENTITY_CONTRACT_PATH':str(contract_path),'HONUA_DEVOPS_PROVISION_APPROVAL_ISSUER_KEYS':'fixture-approver='+base64.b64encode(b'local-fixture-only-key-00000000000').decode()}
pathlib.Path(env['TMPDIR']).mkdir(exist_ok=True)
p=subprocess.Popen([BINARY,'--mcp'],cwd=BASE,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=open(BASE/'record.stderr','w'),text=True,env=env)
def rpc(i,m,params):
 p.stdin.write(json.dumps({'jsonrpc':'2.0','id':i,'method':m,'params':params})+'\n');p.stdin.flush()
 while True:
  if not select.select([p.stdout],[],[],45)[0]:raise RuntimeError('timeout')
  line=p.stdout.readline()
  if not line:raise RuntimeError('operator exited')
  r=json.loads(line)
  if r.get('id')==i:return r['result']
def call(i,name,args):
 r=rpc(i,'tools/call',{'name':name,'arguments':args})
 for c in r.get('content',[]):
  if c.get('type')=='text':
   try:return json.loads(c['text'])
   except ValueError:pass
 return r
try:
 rpc(1,'initialize',{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'fixture-recorder','version':'1'}})
 catalog=rpc(10,'tools/list',{})
 (OUT/'tools-list.json').write_text(json.dumps(catalog,indent=2)+'\n')
 plan=call(2,'provision_infrastructure',{'stack':'aws-ecs','size':'small','action':'plan','variablesJson':json.dumps({'environment':'dev','region':'us-east-1','name_prefix':'honuafixture','redis_enabled':False,'domain_name':'maps.example.test','route53_zone_id':'Z123ABC','honua_image':'ghcr.io/honua-io/honua-server@sha256:'+'a'*64}),'confirmed':False,'confirmation':'','approvalReceiptJson':''})
 (OUT/'plan.json').write_text(json.dumps(plan,indent=2)+'\n');print('plan status:',plan.get('status'),plan.get('summary'))
 question=call(3,'install_honua',{})
 (OUT/'install-intake.json').write_text(json.dumps(question,indent=2)+'\n')
 contract['actions_by_service']['ecs']=['ecs:Create*'];contract_path.write_text(json.dumps(contract))
 refused=call(4,'provision_infrastructure',{'stack':'aws-ecs','size':'small','action':'plan','variablesJson':json.dumps({'environment':'dev','region':'us-east-1','redis_enabled':False,'domain_name':'maps.example.test','route53_zone_id':'Z123ABC'}),'confirmed':False,'confirmation':'','approvalReceiptJson':''})
 (OUT/'wildcard-contract-refusal.json').write_text(json.dumps(refused,indent=2)+'\n');print('wildcard contract:',refused.get('status'))
finally:p.terminate();p.wait(timeout=10)
if plan.get('status')=='terraform-plan-ready':
 issued=subprocess.run([BINARY,'--issue-provision-approval','--from-plan-response',str(OUT/'plan.json'),'--action','apply','--signing-mode','local-hmac-dev','--issuer','fixture-approver'],env=env,capture_output=True,text=True)
 print('issuer exit:',issued.returncode)
 if issued.returncode:print(issued.stderr)
 else:(OUT/'approval-dev.json').write_text(issued.stdout)
