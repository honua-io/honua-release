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
  // Unbound UDP is the exception: native exit succeeds and does not run close.
  for (const [name, field, kind] of [['udp.socket', 'socket', 'UDP'],
    ['child_process', 'process', 'ChildProcess'], ['worker_threads', 'worker', 'Worker']]) {
    channel(name).subscribe(message => {
      const resource = message[field];
      const record = track(resource, kind, undefined);
      if (!record) return;
      const originalUnref = resource.unref;
      record.originalUnref = originalUnref;
      resource.unref = function (...args) {
        mark(record, true);
        return originalUnref.apply(this, args);
      };
      if (kind === 'UDP') {
        const originalRef = resource.ref;
        resource.ref = function (...args) {
          mark(record, false);
          return originalRef.apply(this, args);
        };
        // These calls can leave the socket holding the loop before address()
        // succeeds. addMembership and addSourceSpecificMembership bind implicitly.
        function noteBound() {
          record.bound = true;
          mark(record, record.unreferenced);
        }
        for (const method of ['bind', 'send', 'connect', 'addMembership', 'addSourceSpecificMembership']) {
          const original = resource[method];
          resource[method] = function (...args) {
            const result = original.apply(this, args);
            noteBound();
            return result;
          };
        }
      }
      resource.once(kind === 'UDP' ? 'close' : 'exit', () => forget(record));
    });
  }

  function udpState(socket) {
    try {
      socket.address();
      return 'bound';
    } catch (error) {
      if (error?.code === 'EBADF') return 'unbound';
      if (error?.code === 'ERR_SOCKET_DGRAM_NOT_RUNNING') return 'closed';
      throw error;
    }
  }
  function udpRecords(file) {
    return [...(byFile.get(file) || [])].filter(record => !record.closed && record.kind === 'UDP');
  }
  function udpBound(record) {
    const state = udpState(record.resource);
    if (state === 'closed') return false;
    return record.bound || state === 'bound';
  }

  return {
    idleFailure(file) {
      // Snapshot only sockets that are already bound. An unbound UDP handle can
      // still be listed as UDPWrap; refusing it here would reject native exit.
      for (const record of udpRecords(file)) {
        if (udpState(record.resource) !== 'bound') continue;
        const state = record.unreferenced ? 'unref' : 'dormant';
        return new Error(`unsupported owned ${state} resource: UDP`);
      }
    },
    // beforeExit listeners may bind after the snapshot. A bound unref is still
    // not idle. A bound ref keeps the loop; the raw resource list must say so.
    boundUdpFailure(file) {
      for (const record of udpRecords(file)) {
        if (!udpBound(record) || !record.unreferenced) continue;
        return new Error('unsupported owned unref resource: UDP');
      }
    },
    // Owned work that would keep native Node alive, excluding a dormant unbound
    // UDP socket. That socket is referenced at the handle but does not hold the loop.
    hasReferencedWork(file) {
      for (const record of byFile.get(file) || []) {
        if (record.closed || record.closing) continue;
        if (record.kind === 'UDP') {
          if (udpBound(record) && !record.unreferenced) return true;
          continue;
        }
        const timed = record.kind === 'Timeout' || record.kind === 'Immediate' || record.kind === 'MessagePort';
        if (timed ? record.resource.hasRef() : !record.unreferenced) return true;
      }
      return false;
    },
    dormantUnbound(file) {
      return udpRecords(file).filter(record => !udpBound(record) && !record.unreferenced);
    },
    // Drop the event-loop ref on sockets that are still unbound. That removes
    // their own UDPWrap entry. It does not filter resource types, close the
    // socket, or unref a bound or referenced bound socket.
    detachUnboundUdp(file) {
      for (const record of this.dormantUnbound(file)) {
        record.originalUnref.call(record.resource);
        record.unreferenced = true;
      }
    },
    dispose(file) {
      if (refused.has(file)) throw refused.get(file);
      let pending = false;
      for (const record of byFile.get(file) || []) {
        if (record.closed) continue;
        if (record.closing) { pending = true; continue; }
        const timer = record.kind === 'Timeout' || record.kind === 'Immediate';
        const unreferenced = timer || record.kind === 'MessagePort' ? !record.resource.hasRef() : record.unreferenced;
        if (!unreferenced) continue;
        if (!record.dispose) {
          // Native exit does not run close for an unbound UDP socket. Leave it
          // open. Bound unref, children, and workers stay explicit refusals.
          if (record.kind === 'UDP' && !udpBound(record)) continue;
          throw new Error(`unsupported owned unref resource: ${record.kind}`);
        }
        if (!record.closing) {
          record.closing = true;
          record.dispose();
          if (timer) forget(record);
        }
        // Even synchronous cleanup can invoke a reader callback which creates
        // new referenced work. Require a fresh native idle event before pass.
        pending = true;
      }
      return pending;
    },
  };
}
