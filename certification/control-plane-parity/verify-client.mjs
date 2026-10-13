import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {readFileSync,writeFileSync,realpathSync} from 'node:fs';
import {validateInstalledPin} from './client-identity.mjs';
const lock=JSON.parse(readFileSync('package-lock.json'));
const fromConsumer=createRequire(new URL('./package.json',import.meta.url));
const fromMcp=createRequire(new URL('./node_modules/@honua/mcp-server/package.json',import.meta.url));
const sdkPath=realpathSync(fromConsumer.resolve('@honua/sdk-js'));
assert.equal(realpathSync(fromMcp.resolve('@honua/sdk-js')),sdkPath);
for(const entry of ['@honua/sdk-js/control-plane','@honua/sdk-js/studio']) assert.equal(realpathSync(fromMcp.resolve(entry)),realpathSync(fromConsumer.resolve(entry)));
const load=async(specifier)=>import(pathToFileURL(fromConsumer.resolve(specifier)).href);
assert.equal(typeof (await load('@honua/mcp-server')).createServer,'function');
assert.equal(typeof (await load('@honua/sdk-js/control-plane')).createHonuaCommandRuntime,'function');
assert.equal(typeof (await load('@honua/sdk-js/studio')).createHonuaStudioCommandAdapter,'function');
const expected=JSON.parse(readFileSync('expected-clients.json'));
assert.deepEqual(Object.keys(expected).sort(),['@honua/mcp-server','@honua/sdk-js']);
const records=[];
for(const [name,pin] of Object.entries(expected)) {
 const {version}=pin;
 const installed=lock.packages[`node_modules/${name}`];
 const metadata=await fetch(`https://registry.npmjs.org/${encodeURIComponent(name)}/${version}`).then(r=>{assert(r.ok);return r.json()});
 validateInstalledPin(name,pin,installed,metadata);
 records.push({name,version,integrity:installed.integrity,tarball:installed.resolved,sourceSha:metadata.gitHead});
}
const receipt={kind:'public-npm-default-resolution',clientPackages:records,sharedSdkModule:sdkPath,peerWorkarounds:false,lifecycleScripts:false,scope:'Installed registry clients; no server or command replay yet'};
writeFileSync('provenance.json',JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt));
