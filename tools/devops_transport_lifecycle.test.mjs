// Actual locked SDK+stdio initialization failure, with a real live owned child.
// This source fixture is not a native product/candidate qualification.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, writeFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { fileURLToPath } from 'node:url';
import { createLifecycleTransport } from './devops_transport_lifecycle.mjs';
import { readScopeControlOriginal } from './devops_process_scope.mjs';
import { prepareInstalledSdk } from './devops_native_mcp.mjs';
import { observeProcess, terminalObservation } from './devops_process_observer.mjs';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
test('actual SDK initialize catch and observer expiry cannot close live stdio descendant', async () => {
  const base = join(process.cwd(), 'artifacts/devops-source-controls');
  await mkdir(base, { recursive: true, mode: 0o700 });
  const root = await mkdtemp(join(base, 'lifecycle-'));
  // Unix socket address only; all SDK/kernel originals stay on real artifact disk.
  const socketRoot = await mkdtemp(join(tmpdir(), 'ds-'));
  await writeFile(join(root, 'socket-context.json'), JSON.stringify({ socketRoot, sourceControlOnly: true }) + '\n',
    { mode: 0o600, flag: 'wx' });
  let stock, exited, release = false, server;
  const sockets = new Set();
  try {
    const sdk = await prepareInstalledSdk(root);
    const client = new sdk.Client({ name: 'source-control-only', version: '1' });
    stock = new sdk.StdioClientTransport({ command: '/usr/bin/python3',
      args: [fileURLToPath(new URL('./test_devops_native_scope.py', import.meta.url)), '--initialize-error-worker', root],
      stderr: 'pipe' });
    const lifecycle = createLifecycleTransport(stock);
    await assert.rejects(client.connect(lifecycle.transport, { timeout: 5000 }), /owned-source-initialize-refusal/);
    assert.ok(lifecycle.deferredCloseRequests() >= 1); // actual Client.connect catch
    const launcher = await observeProcess(stock.pid);
    const admission = JSON.parse(await readFile(join(root, 'child.json')));
    await delay(2200); // original stock close would already have sent SIGTERM
    const current = await observeProcess(admission.pid);
    assert.equal(terminalObservation(current, { ...admission.identity, bootId: admission.bootId }).terminal, false);
    const actualLauncher = await observeProcess(launcher.pid);
    assert.equal(terminalObservation(actualLauncher, { ...launcher.identity, bootId: launcher.bootId }).terminal, false);
    let accepted;
    const connected = new Promise(resolve => { accepted = resolve; });
    server = createServer(socket => { sockets.add(socket); socket.once('close', () => sockets.delete(socket)); accepted(); });
    const path = join(socketRoot, 'observer.sock');
    await new Promise((resolve, reject) => { server.once('error', reject); server.listen(path, resolve); });
    const abort = new AbortController();
    const observation = readScopeControlOriginal(path, { sourceControlOnly: true }, abort.signal);
    await connected; abort.abort();
    await assert.rejects(observation, /observer-expired-native-handle-retained/);
    assert.equal(terminalObservation(await observeProcess(admission.pid),
      { ...admission.identity, bootId: admission.bootId }).terminal, false);
    await lifecycle.transport.close(); // explicit SDK/error close is also deferred
    assert.equal(lifecycle.deferredCloseRequests(), 2);
    await assert.rejects(lifecycle.closeAfterTerminal({ closed: true, nativeTerminal: true, launcherTerminal: {} }, admission), /terminal-proof/);
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve)); server = null;
    // Actual normal fixture release and observed transport close, never a kill.
    exited = new Promise(resolve => { const original = stock.onclose; stock.onclose = () => { original?.(); resolve(); }; });
    await writeFile(join(root, 'release.fifo'), 'x'); release = true;
    await exited;
    await stock.close(); // actual child already normally terminal
  } finally {
    for (const socket of sockets) socket.destroy();
    if (server?.listening) await new Promise(resolve => server.close(resolve));
    if (stock?.pid && !release) {
      await writeFile(join(root, 'release.fifo'), 'x');
      // Keep ownership while normal fixture release drains; no timeout/signal.
      while (stock.pid) await delay(20);
    }
    await rm(socketRoot, { recursive: true, force: true });
    // Retain exact SDK verification originals and failure diagnostics on disk.
  }
});
