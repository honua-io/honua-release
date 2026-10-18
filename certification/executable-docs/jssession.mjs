// One public Node REPL per reader context. Never re-evaluate an earlier block.
import repl from 'node:repl';
import { PassThrough, Writable } from 'node:stream';
import { createInterface } from 'node:readline';
import { builtinModules, createRequire, stripTypeScriptTypes } from 'node:module';
import { readFileSync, writeFileSync, appendFileSync, closeSync, openSync, renameSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { Console } from 'node:console';
import { ownResources } from './jsresources.mjs';

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
    // work. Dormant UDP completion remains an explicit partial-support refusal.
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
process.on('unhandledRejection', reason => {
  const reject = evaluations.get(output.getStore());
  if (reject) reject(reason);
  else process.exit(1);
});
const standardGlobals = new Set(Object.getOwnPropertyNames(globalThis));
const server = repl.start({ input, output: new Writable({ write(_chunk, _enc, done) { done(); } }),
  terminal: false, prompt: '', ignoreUndefined: true, replMode: repl.REPL_MODE_STRICT,
  // The default evaluator sends runtime exceptions through the REPL's writer,
  // not always its callback. Capture that public hook rather than swallowing
  // the error output and eventually misclassifying it as a timeout.
  writer(value) {
    const reject = evaluations.get(output.getStore());
    if (reject) reject(value);
    else process.exit(1); // an unowned/background error loses the interpreter
    return '';
  } });
// Interactive Node preloads names such as `fs` on first use. Reader snippets
// must supply their own imports, as their original modules did.
for (const name of builtinModules) {
  if (!standardGlobals.has(name)) delete server.context[name];
}
delete server.context._;
delete server.context._error;
server.context.console = new Console(process.stdout, process.stderr);

// Link the complete leading static-import prefix as one native ESM module before
// any dependency evaluates, then retain its local bindings in the reader REPL.
// Unrecognised syntax fails before evaluation; nothing is replayed.
function imports(source, file) {
  let prefix = '', rest = source;
  const locals = [];
  let count = 0;
  const trivia = /^(?:\s+|\/\/[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/)+/;
  const quoted = `(?:"(?:[^"\\\\\n]|\\\\.)*"|'(?:[^'\\\\\n]|\\\\.)*')`;
  const declaration = new RegExp(`^import\\s+(?:([^;]*?)\\s+from\\s+)?(${quoted})[ \\t]*(?:;|(?=\\r?\\n|$))`);
  const id = '[A-Za-z_$][\\w$]*';
  while (true) {
    const whitespace = rest.match(trivia)?.[0] || '';
    prefix += whitespace;
    rest = rest.slice(whitespace.length);
    if (!/^import\s/.test(rest)) break;
    if (rest.slice('import'.length).replace(trivia, '').startsWith('(')) break;
    const match = rest.match(declaration);
    if (!match) throw new SyntaxError('unsupported static import in reader REPL');
    let binding = match[1]?.trim();
    if (binding) {
      const defaultMatch = binding.match(new RegExp(`^(${id})(?:\\s*,\\s*([\\s\\S]+))?$`));
      if (defaultMatch) { locals.push(defaultMatch[1]); binding = defaultMatch[2] || ''; }
      const namespace = binding.match(new RegExp(`^\\*\\s+as\\s+(${id})$`));
      if (namespace) locals.push(namespace[1]);
      else {
        if (binding) {
          if (!/^\{[\s\S]*\}$/.test(binding)) throw new SyntaxError('unsupported import binding');
          const names = binding.slice(1, -1).split(',').map(x => x.trim());
          if (!names.at(-1)) names.pop();
          for (const name of names) {
            const named = name.match(new RegExp(`^(${id})(?:\\s+as\\s+(${id}))?$`));
            if (!named) throw new SyntaxError('unsupported named import binding');
            locals.push(named[2] || named[1]);
          }
        }
      }
    }
    prefix += match[0] + '\n';
    count++;
    rest = rest.slice(match[0].length);
  }
  if (count === 0) return source;
  const bridge = file + '.imports.mjs';
  writeFileSync(bridge, prefix + `\nexport { ${locals.join(', ')} };\n`);
  const expression = `await import(${JSON.stringify(pathToFileURL(bridge).href)})`;
  return (locals.length ? `const { ${locals.join(', ')} } = ${expression};` : `${expression};`) + rest;
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
      server.context.require = createRequire(file);
      if (file.endsWith('.cjs')) {
        commonjsModule = { id: '.', filename: file, path: path.dirname(file), exports: {}, loaded: false,
          require: server.context.require };
        server.context.module = commonjsModule;
        server.context.exports = commonjsModule.exports;
      } else {
        delete server.context.module;
        delete server.context.exports;
      }
      server.context.__dirname = request.cwd;
      server.context.__filename = file;
      let source = readFileSync(file, 'utf8');
      if (request.language === 'typescript') source = stripTypeScriptTypes(source, { mode: 'transform' });
      source = imports(source, file);
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
          server.eval(source, server.context, file, failure => {
            if (failure) completion.reject(failure);
            else completion.evaluated = true;
          });
        } catch (failure) {
          completion.reject(failure);
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
