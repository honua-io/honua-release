/*
 * S9 — what a demo page actually asked the candidate, and what came back (honua-release#377).
 *
 * Cloud run 38192851328 (aws-serverless) showed why a response listener alone is not evidence: the
 * analyst workbench DID query `/rest/services/honua-workbench/FeatureServer/8/query`, but API Gateway
 * answered with its own 500/503 (Lambda runtime aborted, or throttled), which carries no CORS header.
 * Chromium then reports the call as failed (net::ERR_FAILED) and Playwright emits `requestfailed`, not
 * `response`, so a response-only log was empty and the driver blamed the site's ?apiBase= shim.
 *
 * A CallLog records every request to the candidate (Playwright `request`), the raw status and headers
 * Chromium saw on the wire even when CORS hid the response from the page (CDP
 * `Network.responseReceivedExtraInfo`), and the failure text (`requestfailed`). A 5xx without the
 * server's `Honua-Instance` header is attributed to the gateway: honua-server stamps that header on
 * every response it writes, API Gateway's own `{"message":"Service Unavailable"}` does not.
 *
 * Pure: the drive script feeds it events; the unit tests feed it the same events by hand.
 */

export const INSTANCE_HEADER = "honua-instance";

const lower = (headers) => {
  const out = {};
  for (const [name, value] of Object.entries(headers || {})) out[String(name).toLowerCase()] = value;
  return out;
};

/** Who answered: "server" (the response carries Honua-Instance), "gateway" (a 5xx without it), or
 * "unknown" (no status was observed, or a non-5xx without the header). */
export function answeredBy(status, headers) {
  if (!Number.isInteger(status)) return "unknown";
  const h = lower(headers);
  if (h[INSTANCE_HEADER]) return "server";
  return status >= 500 ? "gateway" : "unknown";
}

export class CallLog {
  constructor(base) {
    this.origin = new URL(base).origin;
    this.calls = [];
    this.byUrl = new Map();
  }

  /** The call for (method, url), the most recent one when the page repeated it. */
  _find(method, url) {
    const list = this.byUrl.get(`${method} ${url}`);
    return list ? list[list.length - 1] : null;
  }

  _ours(url) {
    try {
      return new URL(url).origin === this.origin;
    } catch {
      return false;
    }
  }

  /** Playwright `request`: the page asked the candidate. */
  request(method, url) {
    if (!this._ours(url) || method === "OPTIONS") return null;
    const call = { method, url, path: decodeURIComponent(new URL(url).pathname + new URL(url).search).slice(0, 300),
      status: null, by: "unknown", failure: null };
    this.calls.push(call);
    const key = `${method} ${url}`;
    if (!this.byUrl.has(key)) this.byUrl.set(key, []);
    this.byUrl.get(key).push(call);
    return call;
  }

  /** A status the wire carried: Playwright `response`, or CDP responseReceivedExtraInfo (which
   * Chromium also reports for a CORS-blocked response). */
  status(method, url, status, headers) {
    const call = this._find(method, url);
    if (!call || !Number.isInteger(status)) return;
    call.status = status;
    call.by = answeredBy(status, headers);
  }

  /** Playwright `requestfailed`: the page never got to read the response (CORS, abort, network). */
  failed(method, url, errorText) {
    const call = this._find(method, url);
    if (call) call.failure = String(errorText || "failed").slice(0, 120);
  }

  /** Did the page ask the candidate for anything whose path contains `fragment`? */
  requested(fragment) {
    return this.calls.some((call) => call.path.includes(fragment));
  }

  matching(fragment) {
    return this.calls.filter((call) => call.path.includes(fragment));
  }

  /** The last `n` calls, each described in one line, for evidence. */
  tail(n = 5) {
    return this.calls.slice(-n).map(describeCall);
  }
}

/** `GET /odata/Layers → 503 (API Gateway, no server body)`, `→ 200`, or `→ no response (net::ERR_FAILED)`. */
export function describeCall(call) {
  const head = `${call.method} ${call.path}`;
  if (!Number.isInteger(call.status)) return `${head} → no response${call.failure ? ` (${call.failure})` : ""}`;
  const who = call.by === "gateway" ? " (API Gateway, no server body)" : call.by === "server" ? " (server)" : "";
  const blocked = call.failure ? `; the page could not read it (${call.failure})` : "";
  return `${head} → ${call.status}${who}${blocked}`;
}

const failedCall = (call) => call.failure || !Number.isInteger(call.status) || call.status >= 400;

/** The editing verdict's reason when the data lane fell back to the fixture: the probe
 * (`/odata/Layers`, capability manifest) and features reads that failed, with who answered them. */
export function editingFallbackReason(dataChip, log) {
  const reads = [
    ...log.matching("/odata/Layers").map((call) => ({ call, role: /\/Features/.test(call.path) ? "features" : "probe" })),
    ...log.matching("/api/v1/capabilities/manifest").map((call) => ({ call, role: "probe" })),
  ].filter(({ call }) => failedCall(call));
  const base = `data lane did not go live against the candidate: "${dataChip}"`;
  if (!reads.length) {
    return log.requested("/odata/")
      ? `${base}; every OData read the page made answered without an error status, so the page itself chose the fixture`
      : `${base}; the page never requested /odata/Layers from the candidate`;
  }
  return `${base}; ${reads.map(({ call, role }) => `${role} ${describeCall(call)}`).join("; ")}`;
}

/** The analyst-workbench precondition: did the sample reach the candidate's feature service at all?
 * Returns null when it requested it and at least one request was answered successfully by the server. */
export function workbenchReach(log, service, base) {
  const calls = log.matching(`/rest/services/${service}/`);
  if (!calls.length) {
    return {
      reached: false,
      why: `the workbench never requested the candidate — ?apiBase= did not reach the sample's own baseUrl ` +
        `parameter, so whatever it did was not against ${base}`,
    };
  }
  if (calls.some((call) => !failedCall(call))) return null;
  return {
    reached: true,
    why: `the workbench requested the candidate (${base}) and got ${calls.slice(-3).map(describeCall).join("; ")}`,
  };
}
