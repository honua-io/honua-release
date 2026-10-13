from pathlib import Path
import json,subprocess,sys,hashlib,re
run=Path(__file__).resolve().parent
def query(sql):
 return json.loads(subprocess.check_output(['docker','exec','honua-sdk1424-fixed-db-1','psql','-U','honua','-d','honua','-Atc',sql]))
tables=query("select coalesce(json_agg(t),'[]'::json) from (select table_schema,table_name from information_schema.tables where table_schema='public' and table_name like 'imported_sdk1424%' order by table_name)t")
if sys.argv[1]=='before':
 assert not (run/'import-tables-before.json').exists(),'Retain previous snapshot'
 (run/'import-tables-before.json').write_text(json.dumps(tables,indent=2)+'\n')
else:
 summary=json.loads((Path(sys.argv[1])/'summary.json').read_text())
 case=next(x for x in summary['cases'] if x['user']=='admin-a')
 assert summary['status']=='bounded-parity-passed','Cannot assemble package from failed parity'
 before=json.loads((run/'import-tables-before.json').read_text());added=[x for x in tables if x not in before]
 assert len(added)==1,f'Expected exactly one new import table; got {len(added)}'
 schema,table=added[0]['table_schema'],added[0]['table_name']
 assert re.fullmatch(r'[a-z0-9_]+',schema) and re.fullmatch(r'[a-z0-9_]+',table)
 fc=query(f"select json_build_object('type','FeatureCollection','features',coalesce(json_agg(json_build_object('type','Feature','id',id,'properties',properties,'geometry',ST_AsGeoJSON(geometry)::json) order by id),'[]'::json)) from \"{schema}\".\"{table}\"")
 assert len(fc['features'])==1,'Repeated imports wrote duplicate rows'
 data=(json.dumps(fc,sort_keys=True,separators=(',',':'))+'\n').encode()
 (run/'imported-feature-collection.json').write_bytes(data)
 source=json.loads((run/'owned-import-object.json').read_text())
 proof={'importJobId':case['importJobId'],'operation':case['governedJoin'],'table':added[0],'rowCount':1,'sourceSha256':source['sha256'],'readbackRecordSha256':hashlib.sha256(data).hexdigest(),'readbackMethod':'Direct SQL inspection of owned fixture table; not an SDK published-layer workflow','parityEvidence':str(Path(sys.argv[1]).name)}
 (run/'imported-feature-provenance.json').write_text(json.dumps(proof,indent=2)+'\n')
 print(json.dumps(proof))
