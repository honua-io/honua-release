// Public, block-owned resource cleanup. Never inspect native handle internals.
import { createHook } from 'node:async_hooks';
import { channel } from 'node:diagnostics_channel';
import net from 'node:net';
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import { MessagePort } from 'node:worker_threads';

export function ownResources(scope) {
  const byFile = new Map(), records = new WeakMap(), lifetimes = new Map(), refused = new Map();
  const serverRefState = new WeakMap();
  function track(resource, kind, dispose, unreferenced = false) {
    const file = scope.getStore();
    if (!file || records.has(resource)) return;
    const record = { file, resource, kind, dispose, unreferenced, closed: false, closing: false };
    const entries = byFile.get(file) || new Set();
    byFile.set(file, entries);
    entries.add(record);
    records.set(resource, record);
    return record;
  }
  function forget(record) {
    const release = record.release;
    record.release = undefined;
    release?.();
    record.closed = true;
    if (records.get(record.resource) === record) records.delete(record.resource);
    byFile.get(record.file)?.delete(record);
  }
  function mark(record, unreferenced) {
    const file = scope.getStore();
    if (file && file !== record.file) {
      byFile.get(record.file)?.delete(record);
      const entries = byFile.get(file) || new Set();
      byFile.set(file, entries);
      entries.add(record);
      record.file = file;
    }
    record.unreferenced = unreferenced;
  }
  function refuse(kind) {
    const error = new Error(`unsupported owned unref resource: ${kind}`);
    refused.set(scope.getStore(), error);
    throw error;
  }
  function wrapRef(prototype, name, unreferenced, adopt, states) {
    const original = prototype[name];
    prototype[name] = function (...args) {
      let record = records.get(this);
      if (!record && unreferenced && scope.getStore() && adopt) record = adopt(this);
      if (states) states.set(this, unreferenced);
      if (record) mark(record, unreferenced);
      else if (unreferenced && scope.getStore()) {
        const error = new Error('cannot unref a resource not owned by this reader block');
        refused.set(scope.getStore(), error);
        throw error;
      }
      return original.apply(this, args);
    };
  }
  createHook({
    init(id, kind, _trigger, resource) {
      if (kind === 'MESSAGEPORT' && resource instanceof MessagePort) {
        const record = track(resource, 'MessagePort', () => resource.close());
        // init runs before MessagePort's EventTarget construction completes.
        // Observe identity only; public destroy follows its close lifecycle.
        if (record) lifetimes.set(id, record);
        return;
      }
      if (kind !== 'Timeout' && kind !== 'Immediate') return;
      const previous = records.get(resource);
      if (previous) forget(previous); // refresh() can reactivate the same public Timeout
      const record = track(resource, kind, () =>
        kind === 'Timeout' ? clearTimeout(resource) : clearImmediate(resource));
      if (record) lifetimes.set(id, record);
    },
    destroy(id) {
      const record = lifetimes.get(id);
      if (record) { forget(record); lifetimes.delete(id); }
    },
  }).enable();

  // The documented diagnostic supplies the public Socket, including SDK pools.
  // Only connections created under this block's capture scope become owned.
  const connected = ({ socket }) => {
    const record = track(socket, 'Socket', () => socket.destroy());
    if (record) socket.once('close', () => forget(record));
  };
  channel('net.client.socket').subscribe(connected);
  channel('net.server.socket').subscribe(connected);
  function ownServer(server) {
    const existing = records.get(server);
    if (existing) {
      mark(existing, serverRefState.get(server) === true);
      return existing;
    }
    const record = track(server, 'Server', () => {
      if (server.listening) server.close();
      else forget(record); // an inactive server has no listening handle to stop
    }, serverRefState.get(server) === true);
    if (record) {
      const closed = () => forget(record);
      record.release = () => server.removeListener('close', closed);
      server.once('close', closed);
    }
    return record;
  }
  channel('tracing:net.server.listen:asyncStart').subscribe(({ server }) => ownServer(server));
  wrapRef(net.Socket.prototype, 'unref', true);
  wrapRef(net.Socket.prototype, 'ref', false);
  // Unlike stdin's Socket, the isolated worker has no harness-owned Server.
  // A reader's explicit unref can precede listen; retain that public state for
  // subsequent listen calls on the same server after cleanup has closed it.
  wrapRef(net.Server.prototype, 'unref', true, ownServer, serverRefState);
  wrapRef(net.Server.prototype, 'ref', false, undefined, serverRefState);
  wrapRef(MessagePort.prototype, 'unref', true);
  wrapRef(MessagePort.prototype, 'ref', false);

  const watch = fs.watch;
  fs.watch = function (...args) {
    const watcher = watch.apply(this, args);
    const options = typeof args[1] === 'object' ? args[1] : undefined;
    const record = track(watcher, 'FSWatcher', () => watcher.close(), options?.persistent === false);
    if (record) {
      watcher.once('close', () => forget(record));
      for (const [name, value] of [['unref', true], ['ref', false]]) {
        const original = watcher[name];
        watcher[name] = function (...values) {
          mark(record, value);
          return original.apply(this, values);
        };
      }
    }
    return watcher;
  };
  // These APIs do not expose the same owned close lifecycle as FSWatcher.
  // Refuse their unreferenced forms explicitly rather than leaving stale work.
  const watchFile = fs.watchFile;
  fs.watchFile = function (...args) {
    const file = scope.getStore();
    const options = typeof args[1] === 'object' ? args[1] : undefined;
    if (file && options?.persistent === false) refuse('StatWatcher');
    const watcher = watchFile.apply(this, args);
    if (file) {
      const unref = watcher.unref;
      watcher.unref = function (...values) {
        if (scope.getStore()) refuse('StatWatcher');
        return unref.apply(this, values);
      };
    }
    return watcher;
  };
  const promiseWatch = fs.promises.watch;
  fs.promises.watch = function (...args) {
    if (scope.getStore() && args[1]?.persistent === false) refuse('FSWatchIterator');
    return promiseWatch.apply(this, args);
  };
  syncBuiltinESMExports();

  // These public resources can outlive their process. Until their cleanup is
  // supported, an owned unref is a failure rather than a false completion.
  for (const [name, field, kind] of [['udp.socket', 'socket', 'UDP'],
    ['child_process', 'process', 'ChildProcess'], ['worker_threads', 'worker', 'Worker']]) {
    channel(name).subscribe(message => {
      const resource = message[field];
      const record = track(resource, kind, undefined);
      if (!record) return;
      const original = resource.unref;
      resource.unref = function (...args) {
        mark(record, true);
        return original.apply(this, args);
      };
      resource.once(kind === 'UDP' ? 'close' : 'exit', () => forget(record));
    });
  }

  return {
    dispose(file) {
      if (refused.has(file)) throw refused.get(file);
      let pending = false;
      for (const record of byFile.get(file) || []) {
        if (record.closed) continue;
        if (record.closing) { pending = true; continue; }
        const timer = record.kind === 'Timeout' || record.kind === 'Immediate';
        const unreferenced = timer || record.kind === 'MessagePort' ? !record.resource.hasRef() : record.unreferenced;
        if (!unreferenced) continue;
        if (!record.dispose) throw new Error(`unsupported owned unref resource: ${record.kind}`);
        if (!record.closing) {
          record.closing = true;
          record.dispose();
          if (timer) forget(record);
        }
        pending ||= !record.closed;
      }
      return pending;
    },
  };
}
