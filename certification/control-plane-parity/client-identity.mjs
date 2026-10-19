import assert from 'node:assert/strict';

export function validateInstalledPin(name,pin,installed,metadata) {
 assert(installed,'Selected package must be installed');
 assert.equal(installed.version,pin.version);
 assert.equal(metadata.name,name);
 assert.equal(metadata.version,pin.version);
 assert.equal(metadata.gitHead,pin.sourceSha);
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
