import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {readFileSync,writeFileSync,realpathSync} from 'node:fs';
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
const expected={
 '@honua/sdk-js':{source:'c6484209dda9b1533fefcae1b0e47d87b56414c2',integrity:'sha512-RCiqqRz7sG9qP1xLsLyGroRMhGEi1Nde5YRqYbfkSEbwTSjf1IfciVXgAZdEXz6HDZ/XrzofiK7LIHokgg5j+A=='},
 '@honua/mcp-server':{source:'46ecd25a32ee2bfb7741ccc14ee5842c8824dd19',integrity:'sha512-7yeZUReqE0x6Rdb6Kh7/+3o0CXjCVP/ObPtG5Ay78XkqZgUb9mmguvQ1y4xjNm7YfSp4mW308Ol6ecdRZP3MBg=='}
};
const records=[];
for(const [name,version] of [['@honua/sdk-js','0.1.15'],['@honua/mcp-server','0.1.16']]) {
 const installed=lock.packages[`node_modules/${name}`];assert.equal(installed.version,version);
 assert(installed.resolved.startsWith('https://registry.npmjs.org/'));
 const metadata=await fetch(`https://registry.npmjs.org/${encodeURIComponent(name)}/${version}`).then(r=>{assert(r.ok);return r.json()});
 assert.equal(metadata.gitHead,expected[name].source);assert.equal(installed.integrity,expected[name].integrity);
 assert.equal(metadata.dist.integrity,installed.integrity); assert.equal(metadata.dist.tarball,installed.resolved);
 records.push({name,version,integrity:installed.integrity,tarball:installed.resolved,sourceSha:metadata.gitHead});
}
const receipt={kind:'public-npm-default-resolution',clientPackages:records,sharedSdkModule:sdkPath,peerWorkarounds:false,lifecycleScripts:false,scope:'Installed registry clients; no server or command replay yet'};
writeFileSync('provenance.json',JSON.stringify(receipt,null,2)+'\n');console.log(JSON.stringify(receipt));
