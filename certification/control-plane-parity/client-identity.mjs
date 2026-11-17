import assert from 'node:assert/strict';

const SDK_SOURCE='git+https://github.com/honua-io/honua-sdk-js@';

function statementCommits(payload) {
 try {
  const statement=JSON.parse(Buffer.from(payload,'base64').toString('utf8'));
  return (statement?.predicate?.buildDefinition?.resolvedDependencies??[])
   .filter(dep=>String(dep?.uri??'').startsWith(SDK_SOURCE))
   .map(dep=>String(dep?.digest?.gitCommit??''))
   .filter(commit=>/^[0-9a-f]{40}$/.test(commit));
 } catch { return []; }
}

// npm trusted publishing can omit gitHead; its SLSA provenance still names the commit the
// publish workflow resolved. Only a single commit from the SDK repository counts.
export function attestedCommit(body) {
 const payloads=(body?.attestations??[]).map(a=>a?.bundle?.dsseEnvelope?.payload).filter(p=>typeof p==='string'&&p);
 const found=new Set(payloads.flatMap(statementCommits));
 return found.size===1?[...found][0]:'';
}

// metadata is the registry document plus attestedCommit, which the caller always sets.
export function validateInstalledPin(name,pin,installed,metadata) {
 assert(installed,'Selected package must be installed');
 assert.equal(installed.version,pin.version);
 assert.equal(metadata.name,name);
 assert.equal(metadata.version,pin.version);
 assert.equal(metadata.gitHead||metadata.attestedCommit,pin.sourceSha);
 assert.equal(installed.integrity,pin.integrity);
 assert.equal(metadata.dist.integrity,pin.integrity);
 assert.equal(metadata.dist.tarball,installed.resolved);
 const url=new URL(installed.resolved);
 assert.equal(url.origin,'https://registry.npmjs.org');
 assert.equal(url.pathname.split('/').at(-1),pin.filename);
 assert.equal(url.search,'');
 assert.equal(url.hash,'');
 assert.equal(url.username,'');
 assert.equal(url.password,'');
}
