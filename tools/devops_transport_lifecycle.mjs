// Public Transport composition: SDK timers/protocol and stock stdio stay intact.
// Every SDK close request is deferred until actual owned-scope terminal proof.
import { isScopeWitness } from './devops_process_scope.mjs';
export function createLifecycleTransport(stock) {
  if (typeof stock?.start !== 'function' || typeof stock.send !== 'function' || typeof stock.close !== 'function')
    throw Error('native-lifecycle-stock-transport');
  let deferred = 0, terminal = false, ownedPid = null;
  const transport = {
    async start() { await stock.start(); ownedPid = stock.pid; },
    send(message, options) { return stock.send(message, options); },
    async close() {
      if (!terminal) { deferred++; return; }
      await stock.close();
    },
    get onmessage() { return stock.onmessage; }, set onmessage(value) { stock.onmessage = value; },
    get onclose() { return stock.onclose; }, set onclose(value) { stock.onclose = value; },
    get onerror() { return stock.onerror; }, set onerror(value) { stock.onerror = value; },
    get sessionId() { return stock.sessionId; },
    get pid() { return ownedPid; }
  };
  if (typeof stock.setProtocolVersion === 'function')
    transport.setProtocolVersion = value => stock.setProtocolVersion(value);
  return { transport,
    deferredCloseRequests() { return deferred; },
    async closeAfterTerminal(witness, nativeAdmission) {
      if (!isScopeWitness(witness, nativeAdmission) || !witness.nativeTerminal || !witness.launcherTerminal)
        throw Error('native-lifecycle-terminal-proof');
      terminal = true;
      await transport.close();
    } };
}
