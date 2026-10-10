// JS SDK runner for the interop scenarios.
//
// Runs against the installed, manifest-pinned @honua/sdk-js only and calls its client classes,
// never raw fetch. Reads one JSON request per line on stdin and answers one JSON reply per line on
// stdout. Client instances live for the whole run, so the identity scenario observes a revocation
// on the very client instance that used the key. Replies carry an observation, or an error's type
// and status only; messages go to stderr.
import { readFileSync } from "node:fs";
import { createInterface } from "node:readline";
import { HonuaClient } from "@honua/sdk-js";
import { createHonuaStudioLifecycleClient } from "@honua/sdk-js/studio";

const base = JSON.parse(readFileSync(process.env.SDKREG_PLAN, "utf8")).baseUrl;
const root = new HonuaClient({ baseUrl: base, apiKey: process.env.SDKREG_API_KEY });
// The proposer principal owns the Studio draft and the publication request; it cannot approve them.
const studio = createHonuaStudioLifecycleClient({ client: new HonuaClient({ baseUrl: base, apiKey: process.env.SDKREG_PROPOSER_KEY }) });
const identity = {};

function errorOf(error) {
  const status = [error?.statusCode, error?.status, error?.code].find((value) => Number.isInteger(value));
  process.stderr.write(`[js-runner] ${error?.name ?? typeof error}: ${String(error?.message ?? error).slice(0, 300)}\n`);
  return { type: error?.name ?? "Error", status: status ?? null };
}

// HonuaFeatureLayer.applyEdits(updates): attributes and geometry through the object id another client read.
async function applyEdit(args) {
  const response = await root.featureLayer(args.service, args.layerId).applyEdits({
    updates: [{
      attributes: { [args.objectIdField]: args.objectId, ...args.attributes },
      geometry: { x: args.x, y: args.y, spatialReference: { wkid: 4326 } },
    }],
  });
  return {
    results: (response.updateResults ?? []).map((item) => ({ success: item.success, objectId: item.objectId ?? null, code: item.error?.code ?? null })),
  };
}

// HonuaStudioLifecycleClient.drafts.create
async function createDraft(args) {
  const draft = await studio.drafts.create({ packageKey: args.packageKey, envelope: args.envelope });
  return { draftId: draft?.draftId ?? null, itemId: draft?.itemId ?? null, family: draft?.family ?? draft?.envelope?.family ?? null,
    validation: draft?.envelope?.validation?.status ?? draft?.validation?.status ?? null };
}

// HonuaStudioLifecycleClient.drafts.createContentVersion
async function saveVersion(args) {
  const version = await studio.drafts.createContentVersion(args.draftId);
  return { itemId: version?.itemId ?? null, versionId: version?.versionId ?? null, contentHash: version?.contentHash ?? null };
}

// HonuaStudioLifecycleClient.publicationRequests.create
async function requestPublication(args) {
  const request = await studio.publicationRequests.create(args.itemId, args.versionId, {
    contentHash: args.contentHash,
    intent: { route: args.route, visibility: args.visibility },
  });
  // A direct publication answers 201 with the request itself (`requestId`). The governed path answers 202 with
  // the operation handle, which names the pending publication request in `resourceIds.requestId`
  // (honua-server#5434); approval persists the request under that id.
  const pending = request?.resourceIds?.requestId ?? null;
  return { proposalId: request?.proposalId ?? null, requestId: request?.requestId ?? pending,
    requestIdSource: request?.requestId ? "request" : pending ? "handle.resourceIds" : null, status: request?.status ?? null };
}

// HonuaStudioLifecycleClient.publicationRequests.poll
async function publicationUrl(args) {
  const outcome = await studio.publicationRequests.poll(args.itemId, args.versionId, args.requestId, { timeoutMs: args.timeoutMs });
  return { requestId: args.requestId, state: outcome.state ?? null, active: outcome.active, publicationUrl: outcome.publicationUrl ?? null,
    status: outcome.request?.status ?? null, exhausted: outcome.exhausted ?? null };
}

const identityQuery = async (client, args) =>
  ((await client.featureLayer(args.service, args.layerId).queryFeatures({ where: "1=1", outFields: "*" })).features ?? []).length;

// HonuaClient({apiKey}) with the key the admin CLI minted; the instance is kept for the revocation probe.
async function identityUse(args) {
  identity.client = new HonuaClient({ baseUrl: base, apiKey: readFileSync(args.secretFile, "utf8").trim() });
  return { count: await identityQuery(identity.client, args) };
}

// Same client instance after revocation: count successes until the first refusal, then confirm it holds.
async function identityRevoked(args) {
  const bound = args.observationSeconds;
  const started = Date.now() / 1000;
  let successes = 0;
  for (;;) {
    try {
      await identityQuery(identity.client, args);
    } catch (error) {
      const refused = errorOf(error);
      const after = Math.round((Date.now() / 1000 - (args.revokedAt ?? started)) * 1000) / 1000;
      const confirmations = [];
      for (let index = 0; index < args.confirmations; index += 1) {
        try {
          await identityQuery(identity.client, args);
          confirmations.push(null);
        } catch (error) {
          confirmations.push(errorOf(error).status);
        }
      }
      return { refused: true, status: refused.status, succeededAfterRevocation: successes, refusedAfterSeconds: after, confirmations, observationSeconds: bound };
    }
    successes += 1;
    if (Date.now() / 1000 - started > bound) return { refused: false, succeededAfterRevocation: successes, observationSeconds: bound };
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

const OPS = {
  "apply-edit": applyEdit,
  "studio-create-draft": createDraft,
  "studio-save-version": saveVersion,
  "studio-request-publication": requestPublication,
  "studio-publication-url": publicationUrl,
  "identity-use": identityUse,
  "identity-revoked": identityRevoked,
};

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  const request = JSON.parse(line);
  const reply = { id: request.id };
  try {
    reply.observed = await OPS[request.op](request.args ?? {});
  } catch (error) {
    reply.error = errorOf(error);
  }
  process.stdout.write(JSON.stringify(reply) + "\n");
}
