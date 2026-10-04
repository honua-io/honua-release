// JS SDK driver for the installed-client regression suite.
//
// Runs against the installed, manifest-pinned @honua/sdk-js only and calls its client classes,
// never raw fetch. Prints one JSON observation per contract step; the runner evaluates oracles.
import { readFileSync } from "node:fs";
import { HonuaClient } from "@honua/sdk-js";
import { createHonuaAdminClient } from "@honua/sdk-js/control-plane";

const plan = JSON.parse(readFileSync(process.env.SDKREG_PLAN, "utf8"));
const base = plan.baseUrl;
const apiKey = process.env.SDKREG_API_KEY;
const bearer = process.env.SDKREG_BEARER;
const dbPassword = process.env.SDKREG_DB_PASSWORD;

const API = {
  "sdk-auth": {
    "api-key-query": "HonuaClient({apiKey}).featureLayer().queryFeatures",
    "bearer-admin-list": "HonuaAdminClient({headers: Authorization}).call(listServices)",
    "bearer-query": "HonuaClient({bearerToken}).featureLayer().queryFeatures",
    "anonymous-refused": "HonuaClient({}).featureLayer().queryFeatures",
  },
  "sdk-admin-lifecycle": {
    "create-datasource": "HonuaAdminClient.call(createConnection)",
    "test-datasource": "HonuaAdminClient.call(testConnection)",
    publish: "HonuaAdminClient.call(publishLayer)",
    list: "HonuaAdminClient.call(getPublishedLayers)",
    served: "HonuaClient.featureLayer().queryFeatures",
    unpublish: "HonuaAdminClient.call(setLayerEnabled)",
    "unpublished-refused": "HonuaClient.featureLayer().queryFeatures",
  },
  "sdk-geoservices": {
    query: "HonuaFeatureLayer.queryFeatures",
    ids: "HonuaFeatureLayer.queryObjectIds",
    count: "HonuaFeatureLayer.queryFeatureCount",
    "resolve-edit-ids": "HonuaFeatureLayer.queryFeatures",
    "apply-edits-add": "HonuaFeatureLayer.applyEdits(adds)",
    "apply-edits-update": "HonuaFeatureLayer.applyEdits(updates)",
    "apply-edits-delete": "HonuaFeatureLayer.applyEdits(deletes)",
    "edits-state": "HonuaFeatureLayer.queryFeatures",
    "add-attachment": "HonuaFeatureLayer.addAttachment",
    "query-attachments": "HonuaFeatureLayer.queryAttachments",
  },
  "sdk-ogc-features": {
    "items-bbox": "HonuaClient.listOgcItems",
    item: "HonuaClient.getOgcItem",
  },
  "sdk-ogc-tiles": {
    "vector-tile": "HonuaClient.fetchOgcTile",
    "raster-tile": "HonuaClient.fetchOgcTile(extraParams f=png)",
    "empty-tile": "HonuaClient.fetchOgcTile",
  },
  "sdk-ogc-processes": {
    submit: "HonuaClient.executeOgcProcess(mode async)",
    poll: "HonuaClient.getOgcProcessJob",
    result: "HonuaClient.getOgcProcessJobResults",
  },
  "sdk-stac": { search: "HonuaStacSearch.search" },
};

function emit(scenario, step, payload) {
  process.stdout.write(JSON.stringify({ scenario, step, api: API[scenario][step], ...payload }) + "\n");
}

// Error identity only: type and status. Messages stay in the job log, never in the receipt.
function errorOf(error) {
  const status = [error?.statusCode, error?.status, error?.code].find((value) => Number.isInteger(value));
  process.stderr.write(`[js-driver] ${error?.name ?? typeof error}: ${String(error?.message ?? error).slice(0, 300)}\n`);
  return { type: error?.name ?? "Error", status: status ?? null };
}

async function step(state, scenario, name, action, needs = []) {
  const missing = needs.filter((key) => !(key in state));
  if (missing.length) {
    emit(scenario, name, { skipped: `depends on ${JSON.stringify(missing)}, which did not complete` });
    return;
  }
  try {
    emit(scenario, name, { observed: await action() });
  } catch (error) {
    if (error instanceof Unsupported) {
      emit(scenario, name, { unsupported: error.message });
    } else {
      emit(scenario, name, { error: errorOf(error) });
    }
  }
}

class Unsupported extends Error {}

const dataClient = (auth) => new HonuaClient({ baseUrl: base, ...auth });
const adminClient = (options) => createHonuaAdminClient({ baseUrl: base, ...options });
const unwrap = (result) => (result?.data && typeof result.data === "object" && "data" in result.data ? result.data.data : result.data);

function pointRows(features, fields, oidField) {
  return (features ?? []).map((feature) => {
    const attributes = feature.attributes ?? {};
    const row = {
      attributes: Object.fromEntries(fields.map((field) => [field, attributes[field] ?? null])),
      x: feature.geometry?.x ?? null,
      y: feature.geometry?.y ?? null,
    };
    if (oidField) {
      row.objectId = attributes[oidField] ?? null;
      row.gid = attributes.gid ?? null;
    }
    return row;
  });
}

const editResults = (results) => ({
  results: (results ?? []).map((item) => ({ success: item.success, objectId: item.objectId ?? null, code: item.error?.code ?? null })),
});

// Every row through the layer's query; the count is how many came back.
const rowCount = async (layer) => ({ count: (await layer.queryFeatures({ where: "1=1", outFields: "*" })).features?.length ?? null });

const geojsonPoint = (feature) => ({ x: feature?.geometry?.coordinates?.[0] ?? null, y: feature?.geometry?.coordinates?.[1] ?? null });

async function runAuth(state) {
  const { sites } = plan;
  const scenario = "sdk-auth";
  await step(state, scenario, "api-key-query", async () => rowCount(dataClient({ apiKey }).featureLayer(sites.service, sites.layerId)));
  await step(state, scenario, "bearer-admin-list", async () => {
    const services = unwrap(await adminClient({ headers: { Authorization: `Bearer ${bearer}` } }).call("listServices", {}));
    return { services: (services ?? []).map((service) => service.serviceName) };
  });
  await step(state, scenario, "bearer-query", async () => rowCount(dataClient({ bearerToken: bearer }).featureLayer(sites.service, sites.layerId)));
  await step(state, scenario, "anonymous-refused", async () => {
    const response = await dataClient({}).featureLayer(sites.service, sites.layerId).queryFeatures({ where: "1=1" });
    return { returned: response.features?.length ?? null };
  });
}

async function runAdmin(state) {
  const life = plan.lifecycle;
  const db = life.database;
  const scenario = "sdk-admin-lifecycle";
  const admin = adminClient({ apiKey });
  await step(state, scenario, "create-datasource", async () => {
    const created = unwrap(await admin.call("createConnection", {
      body: {
        name: life.connectionName, host: db.host, port: db.port, databaseName: db.databaseName,
        username: db.username, password: dbPassword, provider: db.provider, sslRequired: false, sslMode: "Disable",
      },
    }));
    state.connection = created.connectionId ?? created.id;
    return { connectionId: state.connection };
  });
  await step(state, scenario, "test-datasource", async () => {
    const result = unwrap(await admin.call("testConnection", { path: { id: state.connection } }));
    return { success: result?.isHealthy ?? result?.success ?? null };
  }, ["connection"]);
  await step(state, scenario, "publish", async () => {
    const published = unwrap(await admin.call("publishLayer", {
      path: { id: state.connection },
      body: {
        schema: "honua_data", table: life.table, layerName: life.layerName, geometryColumn: "geom",
        geometryType: life.geometryType, srid: 4326, primaryKey: "gid", serviceName: life.service, enabled: true,
      },
    }));
    state.layer = published.layerId;
    return { layerId: published.layerId, layerName: published.layerName, serviceName: published.serviceName, enabled: published.enabled };
  }, ["connection"]);
  await step(state, scenario, "list", async () => {
    const layers = unwrap(await admin.call("getPublishedLayers", { path: { id: state.connection }, query: { serviceName: life.service } }));
    return { layers: (layers ?? []).map((layer) => ({ layerId: layer.layerId, enabled: layer.enabled, layerName: layer.layerName })) };
  }, ["layer"]);
  await step(state, scenario, "served", async () => rowCount(dataClient({ apiKey }).featureLayer(life.service, state.layer)), ["layer"]);
  await step(state, scenario, "unpublish", async () => {
    const summary = unwrap(await admin.call("setLayerEnabled", {
      path: { id: state.connection, layerId: state.layer }, query: { serviceName: life.service }, body: { enabled: false },
    }));
    state.unpublished = true;
    return { layerId: summary.layerId, enabled: summary.enabled };
  }, ["layer"]);
  await step(state, scenario, "unpublished-refused", async () => {
    const response = await dataClient({ apiKey }).featureLayer(life.service, state.layer).queryFeatures({ where: "1=1" });
    return { returned: response.features?.length ?? null };
  }, ["unpublished"]);
}

async function runGeoServices(state) {
  const { sites, edits } = plan;
  const scenario = "sdk-geoservices";
  const client = dataClient({ apiKey });
  const sitesLayer = client.featureLayer(sites.service, sites.layerId);
  const editsLayer = client.featureLayer(edits.service, edits.layerId);
  let oidField = "objectid";
  await step(state, scenario, "query", async () => {
    const response = await sitesLayer.queryFeatures({ where: sites.where, outFields: "*" });
    return { features: pointRows(response.features, sites.fields) };
  });
  await step(state, scenario, "ids", async () => ({ ids: await sitesLayer.queryObjectIds({ where: sites.where }) }));
  await step(state, scenario, "count", async () => ({ count: await sitesLayer.queryFeatureCount({ where: sites.where }) }));
  await step(state, scenario, "resolve-edit-ids", async () => {
    const response = await editsLayer.queryFeatures({ where: "1=1", outFields: "*" });
    oidField = response.objectIdFieldName ?? oidField;
    const rows = pointRows(response.features, edits.fields, oidField);
    state.oids = Object.fromEntries(rows.map((row) => [row.gid, row.objectId]));
    return { features: rows.map((row) => ({ gid: row.gid, objectId: row.objectId })) };
  });
  await step(state, scenario, "apply-edits-add", async () => {
    const feature = edits.add;
    const response = await editsLayer.applyEdits({
      adds: [{
        geometry: { x: feature.x, y: feature.y, spatialReference: { wkid: 4326 } },
        attributes: Object.fromEntries(edits.fields.map((field) => [field, feature[field]])),
      }],
    });
    return editResults(response.addResults);
  }, ["oids"]);
  await step(state, scenario, "apply-edits-update", async () => {
    const target = edits.update;
    const response = await editsLayer.applyEdits({
      updates: [{ attributes: { [oidField]: state.oids[target.gid], ...target.attributes } }],
    });
    return editResults(response.updateResults);
  }, ["oids"]);
  await step(state, scenario, "apply-edits-delete", async () => {
    const response = await editsLayer.applyEdits({ deletes: [state.oids[edits.delete.gid]] });
    return editResults(response.deleteResults);
  }, ["oids"]);
  await step(state, scenario, "edits-state", async () => {
    const response = await editsLayer.queryFeatures({ where: "1=1", outFields: "*" });
    return { features: pointRows(response.features, edits.fields) };
  }, ["oids"]);
  await step(state, scenario, "add-attachment", async () => {
    const attachment = edits.attachment;
    const response = await editsLayer.addAttachment({
      objectId: state.oids[attachment.gid],
      attachment: new Blob([attachment.content], { type: attachment.contentType }),
      name: attachment.name,
      contentType: attachment.contentType,
    });
    state.attached = true;
    const result = response.addAttachmentResult ?? response;
    return { results: [{ success: result.success, objectId: result.objectId ?? null }] };
  }, ["oids"]);
  await step(state, scenario, "query-attachments", async () => {
    const objectId = state.oids[edits.attachment.gid];
    const response = await editsLayer.queryAttachments({ objectIds: [objectId] });
    const groups = response.attachmentGroups ?? [];
    const infos = groups.filter((group) => group.parentObjectId === objectId).flatMap((group) => group.attachmentInfos ?? []);
    return { attachments: infos.map((info) => ({ name: info.name, contentType: info.contentType, size: info.size })) };
  }, ["attached"]);
}

async function runOgcFeatures(state) {
  const { sites } = plan;
  const scenario = "sdk-ogc-features";
  const client = dataClient({ apiKey });
  await step(state, scenario, "items-bbox", async () => {
    const response = await client.listOgcItems({ collectionId: sites.collectionId, bbox: sites.bbox.join(","), limit: 100 });
    return { features: (response.features ?? []).map((feature) => ({ id: feature.id, ...geojsonPoint(feature) })) };
  });
  await step(state, scenario, "item", async () => {
    const feature = await client.getOgcItem({ collectionId: sites.collectionId, featureId: sites.itemId });
    return { id: feature.id, properties: feature.properties, ...geojsonPoint(feature) };
  });
}

async function runTiles(state) {
  const { area } = plan;
  const scenario = "sdk-ogc-tiles";
  const client = dataClient({ apiKey });
  const fetchTile = (spec, extra) => client.fetchOgcTile({
    collectionId: area.collectionId, tileMatrixSetId: area.tileMatrixSet,
    tileMatrix: spec.tileMatrix, tileRow: spec.tileRow, tileCol: spec.tileCol, ...extra,
  });
  const encode = (tile) => ({ bytes: Buffer.from(tile.bytes ?? new Uint8Array()).toString("base64"), contentType: tile.contentType });
  await step(state, scenario, "vector-tile", async () => encode(await fetchTile(area.painted)));
  await step(state, scenario, "raster-tile", async () => encode(await fetchTile(area.painted, { extraParams: { f: "png" } })));
  await step(state, scenario, "empty-tile", async () => {
    const tile = await fetchTile(area.empty);
    return { empty: tile.empty === true, size: tile.bytes?.length ?? 0 };
  });
}

async function runProcesses(state) {
  const spec = plan.processes;
  const scenario = "sdk-ogc-processes";
  const client = dataClient({ apiKey });
  await step(state, scenario, "submit", async () => {
    const job = await client.executeOgcProcess({
      processId: spec.processId, mode: "async",
      inputs: { wkb: { type: "Point", coordinates: spec.point }, srid: spec.srid, distance: spec.distance },
    });
    state.job = job.jobID;
    return { jobId: job.jobID, status: job.status };
  });
  await step(state, scenario, "poll", async () => {
    const deadline = Date.now() + spec.pollTimeoutSeconds * 1000;
    let job = await client.getOgcProcessJob({ jobId: state.job });
    while (!["successful", "failed", "dismissed"].includes(job.status) && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      job = await client.getOgcProcessJob({ jobId: state.job });
    }
    if (job.status === "successful") state.succeeded = true;
    return { status: job.status };
  }, ["job"]);
  await step(state, scenario, "result", async () => {
    const results = await client.getOgcProcessJobResults({ jobId: state.job });
    const outputs = results?.outputs ?? results;
    const output = outputs && typeof outputs === "object" ? Object.values(outputs)[0] : null;
    return { geometry: output && typeof output === "object" && "value" in output ? output.value : output };
  }, ["succeeded"]);
}

async function runStac(state) {
  const { sites } = plan;
  await step(state, "sdk-stac", "search", async () => {
    const response = await dataClient({ apiKey }).stac().search({ collections: [sites.collectionId], bbox: sites.bbox, limit: 100 });
    return { features: (response.features ?? []).map((feature) => ({ id: feature.id, ...geojsonPoint(feature) })) };
  });
}

const RUNNERS = {
  "sdk-auth": runAuth,
  "sdk-admin-lifecycle": runAdmin,
  "sdk-geoservices": runGeoServices,
  "sdk-ogc-features": runOgcFeatures,
  "sdk-ogc-tiles": runTiles,
  "sdk-ogc-processes": runProcesses,
  "sdk-stac": runStac,
};

for (const scenario of plan.scenarios) {
  try {
    await RUNNERS[scenario.id]({});
  } catch (error) {
    // A crashed scenario leaves its remaining steps unobserved, which the runner judges as failed.
    process.stderr.write(`[js-driver] scenario ${scenario.id} crashed: ${error?.stack ?? error}\n`);
  }
}
