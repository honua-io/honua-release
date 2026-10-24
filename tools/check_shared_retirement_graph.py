"""Offline native Terraform graph control: synthetic state, no apply or AWS."""
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

out=Path(os.environ.get('HONUA_SHARED_GRAPH_OUTPUT',tempfile.mkdtemp(prefix='honua-retirement-graph-output-')))
out.mkdir(parents=True,exist_ok=True)
source=(Path(__file__).resolve().parents[1]/'certification/hosted-pilot/campaign/shared-retirement.tf').read_text()
match=re.search(r'depends_on\s*=\s*\[([^]]+)\]',source)
assert match and set(re.findall(r'[a-z_]+\.[a-z_]+',match[1])) == {
    'module.application','module.network','aws_security_group.controller','aws_security_group.worker'}
assert re.search(r'when\s*=\s*destroy',source) and 'on_failure' not in source
assert 'exec python3 ../../../tools/hosted_pilot/shared_retirement.py' in source
with tempfile.TemporaryDirectory(prefix='honua-retirement-graph-') as directory:
    root=Path(directory)
    for name in ('application','network'):
        module=root/name;module.mkdir()
        body={'resource':{'terraform_data':{'target':{'input':'synthetic-'+name}}}}
        if name=='application':
            body['variable']={'agent_key':{'type':'string'}}
            body['resource']['terraform_data']['target']['input']='${var.agent_key}'
        (module/'main.tf.json').write_text(json.dumps(body))
    config={'terraform':{'required_version':'=1.13.5'},'module':{
        'network':{'source':'./network'},'application':{'source':'./application','agent_key':'${terraform_data.api.output}'}},
        'resource':{'terraform_data':{
        'controller':{'input':'synthetic-controller','depends_on':['module.network']},
        'worker':{'input':'synthetic-worker','depends_on':['module.network']},
        'api':{'input':'synthetic-api-key-reference'},
        'retirement':{'input':'original-fixed-deadline','depends_on':[
            'module.application','module.network','terraform_data.controller','terraform_data.worker'],
            'provisioner':{'local-exec':{'when':'destroy','command':'exit 71'}}},
        'retained_accounting':{'input':'excluded-retained-accounting'}}}}
    (root/'main.tf.json').write_text(json.dumps(config))
    def resource(name,value,dependencies=None,module=None):
        row={'mode':'managed','type':'terraform_data','name':name,
             'provider':'provider["terraform.io/builtin/terraform"]','instances':[{'schema_version':0,
             'attributes':{'id':'11111111-1111-1111-1111-111111111111','input':{'value':value,'type':'string'},
             'output':{'value':value,'type':'string'},'triggers_replace':None},'sensitive_attributes':[]}]}
        if module:row['module']=module
        if dependencies:row['instances'][0]['dependencies']=dependencies
        return row
    rows=[resource('target','synthetic-api-key-reference',['terraform_data.api'],'module.application'),
          resource('target','synthetic-network',module='module.network'),
          resource('controller','synthetic-controller',['module.network.terraform_data.target']),
          resource('worker','synthetic-worker',['module.network.terraform_data.target']),
          resource('api','synthetic-api-key-reference'),
          resource('retirement','original-fixed-deadline',['module.application.terraform_data.target','module.network.terraform_data.target','terraform_data.controller','terraform_data.worker']),
          resource('retained_accounting','excluded-retained-accounting')]
    state={'version':4,'terraform_version':'1.13.5','serial':1,'lineage':'22222222-2222-2222-2222-222222222222',
           'outputs':{},'resources':rows}
    (root/'terraform.tfstate').write_text(json.dumps(state))
    def run(*args):
        result=subprocess.run(['terraform',*args],cwd=root,capture_output=True,check=False)
        if result.returncode:raise RuntimeError(result.stderr.decode()[:2000])
        return result.stdout
    version=json.loads(run('version','-json'))
    assert version['terraform_version']=='1.13.5'
    run('init','-backend=false','-input=false','-no-color')
    targets=['terraform_data.retirement','module.application','module.network','terraform_data.controller','terraform_data.worker']
    run('plan','-destroy','-refresh=false','-input=false','-no-color',
        *['-target='+target for target in targets],'-out=synthetic.tfplan')
    plan=json.loads(run('show','-json','synthetic.tfplan'))
    changes={row['address']:row['change']['actions'] for row in plan['resource_changes']}
    expected={'module.application.terraform_data.target','module.network.terraform_data.target',
              'terraform_data.controller','terraform_data.worker','terraform_data.retirement'}
    assert expected <= set(changes) <= expected|{'terraform_data.api'},changes
    assert all(actions==['delete'] for actions in changes.values()),changes
    graph=run('graph','-plan=synthetic.tfplan').decode()
    (out/'original-graph.dot').write_text(graph)
    (out/'original-plan-summary.json').write_text(json.dumps({'synthetic':True,'terraformVersion':version['terraform_version'],
        'changes':changes,'applied':False},indent=2)+'\n')
    edges={}
    for origin,destination in re.findall(r'"([^"\n]+)" -> "([^"\n]+)"',graph):
        edges.setdefault(origin,set()).add(destination)
    gate='[root] terraform_data.retirement (destroy)'
    def reaches(node,target,seen=None):
        if node==target:return True
        seen=set() if seen is None else seen
        if node in seen:return False
        return any(reaches(child,target,seen|{node}) for child in edges.get(node,()))
    for address in expected-{'terraform_data.retirement'}:
        assert reaches('[root] '+address+' (destroy)',gate),(address,graph)
    assert 'terraform_data.retained_accounting (destroy)' not in graph
    print('Offline synthetic module/target destroy plan and reverse dependencies PASS; no apply executed.')
    print('Native selected actions:',json.dumps(changes,sort_keys=True))
