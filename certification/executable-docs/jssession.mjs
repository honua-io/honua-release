// One public Node REPL per reader context. Never re-evaluate an earlier block.
import repl from 'node:repl';
import { PassThrough, Writable } from 'node:stream';
import { createInterface } from 'node:readline';
import { builtinModules, createRequire, registerHooks, stripTypeScriptTypes } from 'node:module';
import { readFileSync, writeFileSync, appendFileSync, closeSync, openSync, renameSync, realpathSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { Console } from 'node:console';
import { randomUUID } from 'node:crypto';
import { ownResources } from './jsresources.mjs';
import { ImportBindings } from './jsimports.mjs';

const compilerPath = process.env.HONUA_EXECDOCS_COMPILER;
if (!compilerPath) throw new Error('pinned TypeScript parser was not bootstrapped');
const ts = createRequire(import.meta.url)(compilerPath);
if (ts.version !== '5.9.3') throw new Error(`unexpected TypeScript parser ${ts.version}`);
const importedBindings = new ImportBindings(ts);

// REPL evaluation is a script, so top-level `this` is the global. An ES module's
// top-level `this` is undefined, including arrows. Function, method and class
// `this` stay as written. Splice the original text; do not reprint the block.
function rewriteModuleThis(source) {
  const prefix = 'export {}\n';
  const sf = ts.createSourceFile('block.mjs', prefix + source, ts.ScriptTarget.Latest, true, ts.ScriptKind.JS);
  if (sf.parseDiagnostics?.length) return source;
  const functionLike = new Set([
    ts.SyntaxKind.FunctionDeclaration,
    ts.SyntaxKind.FunctionExpression,
    ts.SyntaxKind.MethodDeclaration,
    ts.SyntaxKind.Constructor,
    ts.SyntaxKind.GetAccessor,
    ts.SyntaxKind.SetAccessor,
    ts.SyntaxKind.ClassStaticBlockDeclaration,
  ]);
  const spans = [];
  let depth = 0;
  const visit = node => {
    if (node.kind === ts.SyntaxKind.ClassDeclaration || node.kind === ts.SyntaxKind.ClassExpression) {
      for (const modifier of node.modifiers ?? []) visit(modifier);
      for (const clause of node.heritageClauses ?? []) visit(clause);
      depth++;
      for (const member of node.members) visit(member);
      depth--;
      return;
    }
    if (functionLike.has(node.kind)) {
      depth++;
      ts.forEachChild(node, visit);
      depth--;
      return;
    }
    if (depth === 0 && node.kind === ts.SyntaxKind.ThisKeyword) {
      const start = node.getStart(sf) - prefix.length;
      const end = node.end - prefix.length;
      if (start >= 0) spans.push([start, end]);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  if (!spans.length) return source;
  let out = '', cursor = 0;
  for (const [start, end] of spans) {
    if (start < cursor || end > source.length || source.slice(start, end) !== 'this') return source;
    out += source.slice(cursor, start) + '(void 0)';
    cursor = end;
  }
  return out + source.slice(cursor);
}

const output = new AsyncLocalStorage();
const initialEnvironment = { ...process.env };
let injectedEnvironment = {};
for (const [stream, suffix] of [[process.stdout, '.out'], [process.stderr, '.err']]) {
  stream.write = (chunk, encoding, callback) => {
    const file = output.getStore();
    if (file) appendFileSync(file + suffix, chunk, typeof encoding === 'string' ? encoding : undefined);
    if (typeof encoding === 'function') encoding();
    else if (callback) callback();
    return true;
  };
  // Only these harness-owned output streams are excluded from liveness. Their
  // actual writes are synchronous, including the redirected descriptors below.
  stream.unref?.();
}
const input = new PassThrough();
const evaluations = new Map();
let evaluating = false;
const refInput = process.stdin.ref.bind(process.stdin);
const unrefInput = process.stdin.unref.bind(process.stdin);
const resources = ownResources(output);
const activeResources = process.getActiveResourcesInfo.bind(process);
let active;
// Node emits beforeExit outside the snippet's async scope. Keep every reader
// listener and the work it schedules attributed to this block, in native order.
const emit = process.emit;
process.emit = function (event, ...args) {
  if (this === process && event === 'beforeExit' && active) {
    // Snapshot at the actual native idle event, before listeners can start new
    // work. Bound UDP is refused here. Unbound UDP is not: native exit allows it.
    active.idleFailure = resources.idleFailure(active.file);
    return output.run(active.file, () => emit.call(this, event, ...args));
  }
  return emit.call(this, event, ...args);
};
function checkpoint() {
  const completion = active;
  if (!completion || completion.checkpoint) return;
  completion.checkpoint = true;
  output.exit(() => {
    const handle = setImmediate(() => {
      handle.unref(); // exclude this exact owned checkpoint, never a type filter
      completion.checkpoint = false;
      if (active !== completion) return;
      output.run(completion.file, () => {
        try {
          // All beforeExit listeners and their nextTick/Promise jobs have run.
          // Reader reference work requires the next natural beforeExit event.
          if (completion.idleFailure) throw completion.idleFailure;
          const boundUdp = resources.boundUdpFailure(completion.file);
          if (boundUdp) throw boundUdp;
          // Referenced work queued by beforeExit, such as a timer that binds
          // later, must be seen before a dormant socket is unref'd. Unref first
          // would turn that bind into an unreferenced bound socket. Native Node
          // keeps the ref and the caller times out. The raw active-resource list
          // is still the completion check; resource types are not removed.
          const listed = activeResources();
          const dormant = resources.dormantUnbound(completion.file);
          if (resources.hasReferencedWork(completion.file) || (dormant.length && listed.length !== dormant.length)) {
            return;
          }
          // Do not close the socket. Drop only the ref of UDP sockets that are
          // still unbound, so their UDPWrap entry leaves the public list.
          resources.detachUnboundUdp(completion.file);
          if (activeResources().length) return;
          const code = Number(process.exitCode) & 255;
          if (code) return completion.reject(new Error(`Node script completed with exit code ${code}`));
          if (!completion.evaluated) return completion.reject(new Error('top-level evaluation did not settle before Node became idle'));
          if (resources.dispose(completion.file)) {
            // Cleanup alone must not manufacture another reader beforeExit.
            // Chain a checkpoint; actual new reference work waits naturally.
            checkpoint();
            return;
          }
          if (activeResources().length) return;
          active = undefined;
          refInput();
          completion.resolve();
        } catch (failure) {
          completion.reject(failure);
        }
      });
    });
  });
}
process.on('beforeExit', checkpoint);
function readerRejectionListeners() {
  return process.listeners('unhandledRejection').filter(listener => listener !== onUnhandledRejection);
}
function onUnhandledRejection(reason) {
  const reject = evaluations.get(output.getStore());
  // Rejections created outside the REPL domain still reach this listener first.
  // Reader listeners registered after it are what native Node treats as handling.
  if (!reject) {
    process.exit(1);
    return;
  }
  if (readerRejectionListeners().length) return;
  reject(reason);
}
process.on('unhandledRejection', onUnhandledRejection);
// Promise rejections the domain swallowed, keyed by the reason object. A primitive
// reason has no identity, so those are counted. Async exceptions are not recorded.
const promiseRejections = new WeakSet();
let primitiveRejections = 0;
let pairedWriterCall = false;
function noteDomainError(error) {
  const site = new Error().stack ?? '';
  // emitUnhandledRejection is the only path native Node would offer to
  // unhandledRejection listeners. Top-level await failures are emitted by the
  // REPL itself, and timer or nextTick throws arrive with domainThrown set.
  if (!site.includes('node:internal/process/promises:')) return;
  if (error !== null && typeof error === 'object') promiseRejections.add(error);
  else primitiveRejections += 1;
}
function takePromiseRejection(value) {
  if (value !== null && typeof value === 'object') {
    if (!promiseRejections.has(value)) return false;
    promiseRejections.delete(value);
    return true;
  }
  if (primitiveRejections === 0) return false;
  primitiveRejections -= 1;
  return true;
}
const standardGlobals = new Set(Object.getOwnPropertyNames(globalThis));
const server = repl.start({ input, output: new Writable({ write(_chunk, _enc, done) { done(); } }),
  terminal: false, prompt: '', ignoreUndefined: true, replMode: repl.REPL_MODE_STRICT,
  // The default evaluator sends runtime exceptions through the REPL's writer,
  // not always its callback. Capture that public hook rather than swallowing
  // the error output and eventually misclassifying it as a timeout.
  writer(value) {
    // Returning '' makes the domain handler call writer again for an Error.
    if (pairedWriterCall) {
      pairedWriterCall = false;
      return '';
    }
    if (value !== null && typeof value === 'object') pairedWriterCall = true;
    // A top-level await continuation is scheduled by the REPL, outside this
    // block's async context. The active completion is still the block.
    const stored = output.getStore();
    const reject = (stored && evaluations.get(stored)) || active?.reject;
    const promiseRejection = !evaluating && takePromiseRejection(value);
    if (reject && promiseRejection && readerRejectionListeners().length) {
      const completion = active;
      if (completion) completion.handledRejection = true;
      try {
        // emit, not listeners(): once() wrappers remove themselves only when
        // EventEmitter delivers the event. Each promise is its own event, even
        // when two rejections share one reason object.
        process.emit('unhandledRejection', value);
      } catch (failure) {
        reject(failure);
        return '';
      }
      if (completion && !completion.evaluated) completion.evaluated = true;
      return '';
    }
    if (reject) reject(value);
    else process.exit(1); // an unowned/background error loses the interpreter
    return '';
  } });
server._domain.prependListener('error', noteDomainError);
// Interactive Node preloads names such as `fs` on first use. Reader snippets
// must supply their own imports, as their original modules did.
for (const name of builtinModules) {
  if (!standardGlobals.has(name)) delete server.context[name];
}
delete server.context._;
delete server.context._error;
server.context.console = new Console(process.stdout, process.stderr);

// Load only a module's native metadata. The loader hook never evaluates the reader
// body, so later snippets still execute it exactly once in their owned REPL.
async function nativeMetadata(file) {
  const url = pathToFileURL(realpathSync(file)).href;
  const privateUrl = url + '?honua-execdocs-metadata=' + randomUUID();
  const hook = registerHooks({
    load(request, context, next) {
      if (request === privateUrl) {
        return { format: 'module', shortCircuit: true, source: 'export default import.meta;' };
      }
      return next(request, context);
    },
  });
  try {
    const meta = (await import(privateUrl)).default;
    meta.url = url;
    meta.main = true;
    return meta;
  } finally {
    hook.deregister();
  }
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const request = JSON.parse(line);
  const file = request.file;
  writeFileSync(file + '.out', '');
  writeFileSync(file + '.err', '');
  await output.run(file, async () => {
    let error, commonjsModule;
    try {
      // Redirect the actual descriptors too: inherited child stdio and native
      // fs.writeSync(1/2) bypass stream.write. Completion uses a separate file.
      for (const [fd, suffix] of [[1, '.out'], [2, '.err']]) {
        closeSync(fd);
        if (openSync(file + suffix, 'a') !== fd) throw new Error(`could not redirect reader fd ${fd}`);
      }
      process.chdir(request.cwd);
      process.exitCode = undefined;
      // A worker may start during temporary source-registry admission. Restore
      // removed injected keys from the container baseline, not that temporary
      // startup environment, without erasing unrelated interpreter mutations.
      const baseline = request.baselineEnv || initialEnvironment;
      for (const key of Object.keys(injectedEnvironment)) {
        if (!Object.hasOwn(request.env, key)) {
          if (Object.hasOwn(baseline, key)) process.env[key] = baseline[key];
          else delete process.env[key];
        }
      }
      Object.assign(process.env, request.env);
      injectedEnvironment = { ...request.env };
      if (file.endsWith('.cjs')) {
        server.context.require = createRequire(file);
        commonjsModule = { id: '.', filename: file, path: path.dirname(file), exports: {}, loaded: false,
          require: server.context.require };
        server.context.module = commonjsModule;
        server.context.exports = commonjsModule.exports;
      } else {
        delete server.context.require;
        delete server.context.module;
        delete server.context.exports;
      }
      server.context.__dirname = request.cwd;
      server.context.__filename = file;
      let source = readFileSync(file, 'utf8');
      if (request.language === 'typescript') source = stripTypeScriptTypes(source, { mode: 'transform' });
      const prepared = importedBindings.prepare(source, file);
      if (prepared.metadataIdentifier) {
        server.context[prepared.metadataIdentifier] = await nativeMetadata(file);
      }
      source = prepared.code;
      if (!file.endsWith('.cjs')) source = rewriteModuleThis(source);
      await new Promise((resolve, reject) => {
        let finished = false;
        const completion = {
          file,
          evaluated: false,
          resolve() { if (!finished) { finished = true; resolve(); } },
          reject(failure) {
            if (finished) return;
            finished = true;
            active = undefined;
            refInput();
            reject(failure);
          },
        };
        evaluations.set(file, completion.reject);
        active = completion;
        unrefInput();
        try {
          evaluating = true;
          server.eval(source, server.context, file, failure => {
            if (failure) {
              if (completion.handledRejection) {
                completion.evaluated = true;
                return;
              }
              completion.reject(failure);
            } else completion.evaluated = true;
          });
        } catch (failure) {
          completion.reject(failure);
        } finally {
          evaluating = false;
        }
      });
      if (commonjsModule) commonjsModule.loaded = true;
    } catch (failure) {
      error = failure?.stack || String(failure);
      process.stderr.write(error + '\n');
    } finally {
      evaluations.delete(file);
    }
    const response = JSON.stringify({ file, id: request.id, ok: error === undefined,
      lost: error !== undefined, error: error?.slice(-4000) || '',
      exitCode: error === undefined ? 0 : (Number(process.exitCode) & 255) || 1 });
    writeFileSync(request.response + '.tmp', response);
    renameSync(request.response + '.tmp', request.response);
    // A failed ESM script terminates rather than running later event-loop turns.
    // The rejection's already-queued microtasks have retained their native effects.
    if (error !== undefined) process.exit(1);
  });
}
server.close();
input.end();
