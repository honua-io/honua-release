// One public Node REPL per reader context. Never re-evaluate an earlier block.
import repl from 'node:repl';
import { PassThrough, Writable } from 'node:stream';
import { createInterface } from 'node:readline';
import { builtinModules, createRequire, stripTypeScriptTypes } from 'node:module';
import { readFileSync, writeFileSync, appendFileSync } from 'node:fs';
import { AsyncLocalStorage } from 'node:async_hooks';
import path from 'node:path';
import { Console } from 'node:console';

const reply = process.stdout.write.bind(process.stdout);
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
}
const input = new PassThrough();
const evaluations = new Map();
const refInput = process.stdin.ref.bind(process.stdin);
const unrefInput = process.stdin.unref.bind(process.stdin);
let active;
// With only our input pipe unreferenced, beforeExit is Node's own completion
// oracle: referenced timers, I/O and their promise callbacks have drained.
// Re-reference the pipe before replying so the next block can arrive normally.
process.on('beforeExit', () => {
  if (!active) return;
  const completion = active;
  active = undefined;
  refInput();
  if (completion.evaluated) completion.resolve();
  else completion.reject(new Error('top-level evaluation did not settle before Node became idle'));
});
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

// REPLs accept dynamic imports. Adapt the leading static import declarations that
// reader snippets use, retaining lexical bindings. Unrecognised syntax fails
// before evaluation; it is never guessed, concatenated or retried.
function imports(source) {
  let prefix = '', rest = source;
  const trivia = /^(?:\s+|\/\/[^\n]*(?:\n|$)|\/\*[\s\S]*?\*\/)+/;
  const quoted = `(?:"(?:[^"\\\\\n]|\\\\.)*"|'(?:[^'\\\\\n]|\\\\.)*')`;
  const declaration = new RegExp(`^import\\s+(?:([^;]*?)\\s+from\\s+)?(${quoted})[ \\t]*(?:;|(?=\\r?\\n|$))`);
  const id = '[A-Za-z_$][\\w$]*';
  while (true) {
    const whitespace = rest.match(trivia)?.[0] || '';
    prefix += whitespace;
    rest = rest.slice(whitespace.length);
    if (!/^import\s/.test(rest)) break;
    const match = rest.match(declaration);
    if (!match) throw new SyntaxError('unsupported static import in reader REPL');
    let binding = match[1]?.trim();
    const expression = `await import(${match[2]})`;
    if (!binding) prefix += `${expression};`;
    else {
      let defaultName;
      const defaultMatch = binding.match(new RegExp(`^(${id})(?:\\s*,\\s*([\\s\\S]+))?$`));
      if (defaultMatch) { defaultName = defaultMatch[1]; binding = defaultMatch[2] || ''; }
      const entries = [];
      if (defaultName) entries.push(`default: ${defaultName}`);
      const namespace = binding.match(new RegExp(`^\\*\\s+as\\s+(${id})$`));
      if (namespace) {
        prefix += `const ${namespace[1]} = ${expression};`;
        if (defaultName) prefix += `const { default: ${defaultName} } = ${namespace[1]};`;
      } else {
        if (binding) {
          if (!/^\{[\s\S]*\}$/.test(binding)) throw new SyntaxError('unsupported import binding');
          const names = binding.slice(1, -1).split(',').map(x => x.trim());
          if (!names.at(-1)) names.pop();
          for (const name of names) {
            const named = name.match(new RegExp(`^(${id})(?:\\s+as\\s+(${id}))?$`));
            if (!named) throw new SyntaxError('unsupported named import binding');
            entries.push(`${named[1]}: ${named[2] || named[1]}`);
          }
        }
        prefix += `const { ${entries.join(', ')} } = ${expression};`;
      }
    }
    rest = rest.slice(match[0].length);
  }
  return prefix + rest;
}

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const request = JSON.parse(line);
  const file = request.file;
  writeFileSync(file + '.out', '');
  writeFileSync(file + '.err', '');
  await output.run(file, async () => {
    let error;
    try {
      process.chdir(request.cwd);
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
      server.context.require = createRequire(path.join(request.cwd, '__doc_reader.cjs'));
      server.context.__dirname = request.cwd;
      server.context.__filename = file;
      let source = readFileSync(file, 'utf8');
      if (request.language === 'typescript') source = stripTypeScriptTypes(source, { mode: 'transform' });
      source = imports(source);
      await new Promise((resolve, reject) => {
        let finished = false;
        const completion = {
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
    } catch (failure) {
      error = failure?.stack || String(failure);
      process.stderr.write(error + '\n');
    } finally {
      evaluations.delete(file);
    }
    reply(JSON.stringify({ file, ok: error === undefined, lost: error !== undefined,
      error: error?.slice(-4000) || '' }) + '\n');
  });
}
server.close();
input.end();
