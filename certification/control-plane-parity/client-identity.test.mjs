import assert from 'node:assert/strict';
import test from 'node:test';
import {validateInstalledPin} from './client-identity.mjs';

const name='@honua/sdk-js';
const pin={version:'0.1.16',sourceSha:'a'.repeat(40),integrity:'sha512-reviewed',filename:'sdk-js-0.1.16.tgz'};
const installed={version:pin.version,integrity:pin.integrity,resolved:'https://registry.npmjs.org/@honua/sdk-js/-/'+pin.filename};
const metadata={name,version:pin.version,gitHead:pin.sourceSha,dist:{integrity:pin.integrity,tarball:installed.resolved}};

test('installed metadata must match every reviewed identity',()=>{
 validateInstalledPin(name,pin,installed,metadata);
 for(const field of ['version','integrity']) assert.throws(()=>validateInstalledPin(name,pin,{...installed,[field]:'different'},metadata));
 for(const field of ['name','version','gitHead']) assert.throws(()=>validateInstalledPin(name,pin,installed,{...metadata,[field]:'different'}));
 for(const field of ['integrity','tarball']) assert.throws(()=>validateInstalledPin(name,pin,installed,{...metadata,dist:{...metadata.dist,[field]:'different'}}));
 assert.throws(()=>validateInstalledPin(name,pin,undefined,metadata));
});

test('matching lock and metadata cannot substitute a foreign or differently named tarball',()=>{
 for(const url of ['https://registry.example.org/'+pin.filename,'http://registry.npmjs.org/'+pin.filename,'https://registry.npmjs.org/other.tgz','https://registry.npmjs.org/'+pin.filename+'?override=1','https://user@registry.npmjs.org/'+pin.filename]) {
  assert.throws(()=>validateInstalledPin(name,pin,{...installed,resolved:url},{...metadata,dist:{...metadata.dist,tarball:url}}));
 }
});
