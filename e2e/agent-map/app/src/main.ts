/**
 * Maui live map — an agent-drivable map built only from the published
 * `@honua/sdk-js` surface:
 *
 *   connect()            root entrypoint      — discover each public FeatureServer layer
 *   mountSource()        @honua/sdk-js/map    — data-to-map bridge onto a caller-owned MapLibre map
 *   renderers            @honua/sdk-js/style  — classBreaks / uniqueValue / heatmap
 *   createHonuaAiMapKit  @honua/sdk-js/agent-tools — the bounded agent tool plane
 *
 * The runtime adapter below is the "your own object over a MapLibre map"
 * option the nl-map-control doc describes. Every agent tool call lands here
 * through the kit's executor, is audited, and is shown in the activity panel.
 */
import "./maplibre-worker.js";
import "maplibre-gl/dist/maplibre-gl.css";
import "./styles.css";

import { connect, envelope } from "@honua/sdk-js";
import type { HonuaConnection, Query, QueryFilterExpression } from "@honua/sdk-js";
import { createHonuaAiMapKit } from "@honua/sdk-js/agent-tools";
import type {
  HonuaAgentLayerSummary,
  HonuaAgentLayerStyle,
  HonuaAgentRuntime,
  HonuaAgentSourceSummary,
  HonuaAgentToolCall,
  HonuaAgentToolResult,
  HonuaAgentViewport,
  SetVisibilityArgs,
} from "@honua/sdk-js/agent-tools";
import type { FeatureSelectionTarget, FilterClause } from "@honua/sdk-js/exploration";
import { mountSource } from "@honua/sdk-js/map";
import type { MountedSource } from "@honua/sdk-js/map";
import { classBreaksRenderer, heatmapRenderer, uniqueValueRenderer } from "@honua/sdk-js/style";
import type { Renderer } from "@honua/sdk-js/style";
import { defineHonuaWebComponents } from "@honua/sdk-js/web-components";
import type {
  HonuaLayerListElement,
  HonuaLayerModel,
  HonuaLayerVisibilityChangeDetail,
  HonuaLegendElement,
  HonuaLegendItem,
} from "@honua/sdk-js/web-components";
import * as maplibregl from "maplibre-gl";

// Registers <honua-map>, <honua-layer-list>, <honua-legend>, <honua-bookmarks>, … from the SDK.
defineHonuaWebComponents();

// ---------------------------------------------------------------------------
// Catalog — exactly what `honua_list_sources` on https://demo.honua.io returned
// through @honua/mcp-server (protocol-neutral `<protocol>:<address>` refs).
// ---------------------------------------------------------------------------

const DEMO_SERVICES = "https://demo.honua.io/rest/services";

interface CatalogEntry {
  readonly id: string;
  readonly title: string;
  readonly mcpSource: string;
  readonly endpoint: string;
}

const CATALOG: readonly CatalogEntry[] = [
  ["maui-parcels", 1, "Maui parcels (TMK)"],
  ["maui-zoning", 2, "Maui zoning districts"],
  ["maui-roads", 3, "Maui roads"],
  ["maui-flood-hazard", 4, "FEMA flood hazard zones"],
  ["maui-sea-level-rise", 5, "Sea level rise exposure"],
  ["maui-place-names", 6, "Place names"],
  ["maui-inspections", 12, "Building inspections"],
  ["maui-buildings", 13, "Building footprints"],
].map(([id, layer, title]) => ({
  id: String(id),
  title: String(title),
  mcpSource: `geoservices-feature-service:${id}/${layer}`,
  endpoint: `${DEMO_SERVICES}/${id}/FeatureServer/${layer}`,
}));

const BASEMAP_STYLE = "https://tiles.openfreemap.org/styles/liberty";
const MAUI_CENTER: [number, number] = [-156.33, 20.8];

// ---------------------------------------------------------------------------
// Activity panel
// ---------------------------------------------------------------------------

interface ActivityEntry {
  readonly seq: number;
  readonly at: string;
  readonly tool: string;
  readonly args: unknown;
  readonly status: string;
  readonly ms: number;
  readonly detail?: string;
}

const activity: ActivityEntry[] = [];

function el(id: string): HTMLElement {
  const found = document.getElementById(id);
  if (!found) throw new Error(`Missing #${id}`);
  return found;
}

function renderActivity(): void {
  const list = el("activity");
  list.replaceChildren(
    ...activity
      .slice()
      .reverse()
      .map((entry) => {
        const item = document.createElement("li");
        item.dataset["status"] = entry.status;
        const head = document.createElement("div");
        head.className = "entry-head";
        // Tool names and statuses come from model-driven calls: build text nodes, never HTML.
        const parts: Array<[string, string]> = [
          ["span.seq", `#${entry.seq}`],
          ["code", entry.tool],
          ["span.status", entry.status],
          ["span.ms", `${entry.ms} ms`],
        ];
        for (const [selector, text] of parts) {
          const [tag, className] = selector.split(".");
          const node = document.createElement(tag!);
          if (className) node.className = className;
          node.textContent = text;
          head.append(node);
        }
        const args = document.createElement("pre");
        args.textContent = JSON.stringify(entry.args ?? {}, null, 0).slice(0, 220);
        item.append(head, args);
        if (entry.detail) {
          const detail = document.createElement("p");
          detail.className = "detail";
          detail.textContent = entry.detail;
          item.append(detail);
        }
        return item;
      }),
  );
}

function renderLayers(): void {
  const list = el("layers");
  list.replaceChildren(
    ...[...mounted.values()].map((entry) => {
      const item = document.createElement("li");
      item.dataset["visible"] = String(entry.visible);
      const count = entry.handle.diagnostics.featureCount ?? 0;
      const overflow = entry.handle.diagnostics.overflow;
      // Titles and source ids are agent-supplied: build text nodes, never HTML.
      const title = document.createElement("strong");
      title.textContent = entry.title;
      const source = document.createElement("span");
      source.textContent = entry.sourceId;
      const counts = document.createElement("span");
      counts.textContent = `${count}${overflow ? ` of ${overflow.totalCount}` : ""} features${entry.visible ? "" : " · hidden"}`;
      item.append(title, source, counts);
      return item;
    }),
  );
  el("layer-count").textContent = String(mounted.size);
}

// ---------------------------------------------------------------------------
// Map + connections
// ---------------------------------------------------------------------------

const map = new maplibregl.Map({
  container: "map",
  style: BASEMAP_STYLE,
  center: MAUI_CENTER,
  zoom: 9.2,
  attributionControl: { compact: true },
  canvasContextAttributes: { preserveDrawingBuffer: true },
});
map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-left");
map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-left");

const connections = new Map<string, HonuaConnection>();
const connectionErrors = new Map<string, string>();

async function connection(sourceId: string): Promise<HonuaConnection> {
  const cached = connections.get(sourceId);
  if (cached) return cached;
  const entry = CATALOG.find((candidate) => candidate.id === sourceId);
  if (!entry) throw new Error(`Unknown source "${sourceId}". Known: ${CATALOG.map((c) => c.id).join(", ")}`);
  const opened = await connect({
    endpoint: entry.endpoint,
    protocol: "geoservices-feature-service",
    authorizationScopeFingerprint: "anonymous-public",
  });
  connections.set(sourceId, opened);
  return opened;
}

interface MountedLayer {
  readonly id: string;
  readonly sourceId: string;
  readonly title: string;
  readonly baseQuery: Query;
  visible: boolean;
  handle: MountedSource;
  renderer?: Renderer;
}

const mounted = new Map<string, MountedLayer>();

// ---------------------------------------------------------------------------
// Widgets — the SDK's own web components docked over the map.
// ---------------------------------------------------------------------------

type WidgetKind = "layer-list" | "legend";
const WIDGET_KIND_ALIASES: Readonly<Record<string, WidgetKind>> = {
  "layer-list": "layer-list",
  layerlist: "layer-list",
  layers: "layer-list",
  toc: "layer-list",
  "table-of-contents": "layer-list",
  legend: "legend",
};

interface DockedWidget {
  readonly id: string;
  readonly kind: WidgetKind;
  readonly title: string;
  readonly card: HTMLElement;
  readonly element: HonuaLayerListElement | HonuaLegendElement;
}

const widgets = new Map<string, DockedWidget>();

function layerRows(): HonuaLayerModel[] {
  return [...mounted.values()].reverse().map((entry) => ({
    id: entry.id,
    title: entry.title,
    sourceId: entry.sourceId,
    type: entry.handle.strategy,
    visible: entry.visible,
    metadata: { featureCount: entry.handle.diagnostics.featureCount },
  }));
}

function readColor(layerId: string, property: string): string | undefined {
  try {
    const value = map.getPaintProperty(layerId, property as never);
    return typeof value === "string" ? value : undefined;
  } catch {
    return undefined;
  }
}

function legendItems(): HonuaLegendItem[] {
  const items: HonuaLegendItem[] = [];
  for (const entry of mounted.values()) {
    if (entry.renderer) {
      for (const [index, item] of entry.renderer.legendItems().entries()) {
        items.push({
          id: `${entry.id}:${index}`,
          label: item.label,
          layerId: entry.id,
          ...(item.color ? { color: item.color } : {}),
          ...("minValue" in item && typeof item.minValue === "number" ? { minValue: item.minValue } : {}),
          ...("maxValue" in item && typeof item.maxValue === "number" ? { maxValue: item.maxValue } : {}),
        });
      }
      continue;
    }
    const color =
      readColor(`${entry.id}-polygon`, "fill-color") ??
      readColor(`${entry.id}-line`, "line-color") ??
      readColor(`${entry.id}-point`, "circle-color");
    items.push({ id: entry.id, label: entry.title, layerId: entry.id, ...(color ? { color } : {}) });
  }
  return items;
}

function renderWidgets(): void {
  const rows = layerRows();
  const items = legendItems();
  for (const widget of widgets.values()) {
    if (widget.kind === "layer-list") (widget.element as HonuaLayerListElement).layers = rows;
    else (widget.element as HonuaLegendElement).items = items;
  }
}

function dockWidget(spec: { id: string; kind: string; title?: string }): DockedWidget {
  const kind = WIDGET_KIND_ALIASES[spec.kind.toLowerCase().replace(/[^a-z-]/g, "")];
  if (!kind) {
    throw new Error(
      `Widget kind "${spec.kind}" is not available in this runtime. Available: ${Object.keys(WIDGET_KIND_ALIASES).join(", ")} (SDK <honua-layer-list> and <honua-legend>).`,
    );
  }
  if (widgets.has(spec.id)) throw new Error(`Widget "${spec.id}" already exists`);
  const card = document.createElement("section");
  card.className = "widget-card";
  card.dataset["widget"] = spec.id;
  const heading = document.createElement("h3");
  heading.textContent = spec.title ?? (kind === "layer-list" ? "Layers" : "Legend");
  const element = document.createElement(kind === "layer-list" ? "honua-layer-list" : "honua-legend") as
    | HonuaLayerListElement
    | HonuaLegendElement;
  if (kind === "layer-list") {
    element.addEventListener("honua-layer-visibility-change", (event) => {
      const detail = (event as CustomEvent<HonuaLayerVisibilityChangeDetail>).detail;
      void execute({
        name: "setVisibility",
        args: { draftId: "local", generation: 0, layerId: detail.layerId, visible: detail.visible },
      });
    });
  }
  card.append(heading, element);
  el("widgets").append(card);
  const widget: DockedWidget = { id: spec.id, kind, title: heading.textContent ?? spec.id, card, element };
  widgets.set(spec.id, widget);
  renderWidgets();
  return widget;
}
const filters = new Map<string, FilterClause>();
let selection: FeatureSelectionTarget[] = [];

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function describe(value: unknown): Record<string, unknown> {
  // Bounded, secret-free projection of a source descriptor for the agent.
  const descriptor = (value ?? {}) as Record<string, unknown>;
  const schema = descriptor["schema"] as Record<string, unknown> | undefined;
  const fields = Array.isArray(schema?.["fields"])
    ? (schema?.["fields"] as ReadonlyArray<Record<string, unknown>>)
        .slice(0, 24)
        .map((field) => `${String(field["name"])}:${String(field["type"] ?? "?")}`)
    : undefined;
  return {
    ...(descriptor["geometryType"] !== undefined ? { geometryType: descriptor["geometryType"] } : {}),
    ...(schema?.["geometryType"] !== undefined ? { geometryType: schema["geometryType"] } : {}),
    ...(fields ? { fields } : {}),
    ...(schema?.["primaryKey"] !== undefined ? { primaryKey: schema["primaryKey"] } : {}),
    ...(descriptor["extent"] !== undefined ? { extent: descriptor["extent"] } : {}),
  };
}

function clauseToFilter(clause: FilterClause): QueryFilterExpression {
  const property = { kind: "property" as const, name: clause.field };
  const literal = (value: unknown) => ({ kind: "literal" as const, value: value as string | number | boolean | null });
  switch (clause.operator) {
    case "=":
      return { kind: "comparison", operator: "eq", left: property, right: literal(clause.value) };
    case "!=":
      return { kind: "comparison", operator: "ne", left: property, right: literal(clause.value) };
    case "<":
      return { kind: "comparison", operator: "lt", left: property, right: literal(clause.value) };
    case "<=":
      return { kind: "comparison", operator: "lte", left: property, right: literal(clause.value) };
    case ">":
      return { kind: "comparison", operator: "gt", left: property, right: literal(clause.value) };
    case ">=":
      return { kind: "comparison", operator: "gte", left: property, right: literal(clause.value) };
    case "in":
      return { kind: "list", operator: "in", operand: property, values: (clause.value as unknown[]).map(literal) };
    case "not-in":
      return {
        kind: "not",
        arg: { kind: "list", operator: "in", operand: property, values: (clause.value as unknown[]).map(literal) },
      };
    case "between": {
      const [lower, upper] = clause.value as [unknown, unknown];
      return { kind: "range", operator: "between", operand: property, lower: literal(lower), upper: literal(upper) };
    }
    case "like":
      return { kind: "pattern", operator: "like", operand: property, pattern: String(clause.value), caseSensitive: false };
    case "is-null":
      return { kind: "null", operator: "is-null", operand: property };
    case "is-not-null":
      return { kind: "null", operator: "is-not-null", operand: property };
  }
}

function combinedFilter(sourceId: string): QueryFilterExpression | undefined {
  const applicable = [...filters.values()].filter(
    (clause) => !clause.appliesTo || clause.appliesTo.length === 0 || clause.appliesTo.includes(sourceId),
  );
  if (applicable.length === 0) return undefined;
  if (applicable.length === 1) return clauseToFilter(applicable[0]!);
  return { kind: "boolean", operator: "and", args: applicable.map(clauseToFilter) };
}

function rendererFrom(spec: unknown): Renderer | undefined {
  if (typeof spec !== "object" || spec === null) return undefined;
  const value = spec as Record<string, unknown>;
  switch (value["kind"]) {
    case "class-breaks":
      return classBreaksRenderer(value as never);
    case "unique-value":
      return uniqueValueRenderer(value as never);
    case "heatmap":
      return heatmapRenderer(value as never);
    default:
      throw new Error('renderer.kind must be "class-breaks", "unique-value", or "heatmap"');
  }
}

function viewport(): HonuaAgentViewport {
  const bounds = map.getBounds();
  const center = map.getCenter();
  return {
    bbox: [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()],
    center: [center.lng, center.lat],
    zoom: map.getZoom(),
    pitch: map.getPitch(),
    bearing: map.getBearing(),
    crs: "EPSG:4326",
  };
}

function afterMove(): Promise<void> {
  return new Promise((resolve) => {
    if (!map.isMoving()) {
      resolve();
      return;
    }
    map.once("moveend", () => resolve());
  });
}

// ---------------------------------------------------------------------------
// The HonuaAgentRuntime adapter — the only thing the agent can act through.
// ---------------------------------------------------------------------------

const runtime: HonuaAgentRuntime = {
  id: "maui-live-map",

  async listSources(): Promise<ReadonlyArray<HonuaAgentSourceSummary>> {
    await Promise.allSettled(
      CATALOG.map(async (entry) => {
        try {
          await connection(entry.id);
        } catch (error) {
          connectionErrors.set(entry.id, error instanceof Error ? error.message : String(error));
        }
      }),
    );
    return CATALOG.map((entry) => {
      const opened = connections.get(entry.id);
      const source = opened?.source();
      const capabilities = source?.capabilities;
      return {
        id: entry.id,
        title: entry.title,
        protocol: "geoservices-feature-service",
        ...(Array.isArray(capabilities) ? { capabilities } : {}),
        metadata: {
          mcpSource: entry.mcpSource,
          endpoint: entry.endpoint,
          ...(source ? describe(source.descriptor) : {}),
          ...(connectionErrors.has(entry.id) ? { connectionError: connectionErrors.get(entry.id) } : {}),
        },
      };
    });
  },

  listLayers(): ReadonlyArray<HonuaAgentLayerSummary> {
    return [...mounted.values()].map((entry) => ({
      id: entry.id,
      sourceId: entry.sourceId,
      title: entry.title,
      visible: entry.visible,
      type: entry.handle.strategy,
      metadata: {
        maplibreLayerIds: entry.handle.layerIds,
        featureCount: entry.handle.diagnostics.featureCount,
        ...(entry.handle.diagnostics.overflow ? { overflow: entry.handle.diagnostics.overflow } : {}),
      },
    }));
  },

  getViewport: () => viewport(),

  async setViewport(next: HonuaAgentViewport) {
    if (next.bbox) {
      map.fitBounds(next.bbox as [number, number, number, number], { padding: 48, duration: 1400 });
    } else {
      map.easeTo({
        ...(next.center ? { center: next.center as [number, number] } : {}),
        ...(next.zoom !== undefined ? { zoom: next.zoom } : {}),
        ...(next.pitch !== undefined ? { pitch: next.pitch } : {}),
        ...(next.bearing !== undefined ? { bearing: next.bearing } : {}),
        duration: 1400,
      });
    }
    await afterMove();
    return viewport();
  },

  async addLayer(layer, beforeId) {
    const id = String(layer["id"] ?? "");
    const sourceId = String(layer["sourceId"] ?? id);
    if (!id) throw new Error("addLayer requires layer.id");
    if (mounted.has(id)) throw new Error(`Layer "${id}" already exists`);
    const opened = await connection(sourceId);
    const source = opened.source();
    const limit = typeof layer["limit"] === "number" ? (layer["limit"] as number) : 2000;
    const bbox = Array.isArray(layer["bbox"]) ? (layer["bbox"] as [number, number, number, number]) : undefined;
    const baseQuery: Query = {
      pagination: { limit },
      returnGeometry: true,
      outSr: 4326,
      ...(bbox ? { spatialFilter: envelope(bbox[0], bbox[1], bbox[2], bbox[3]) } : {}),
      ...(typeof layer["where"] === "string" ? { where: layer["where"] as string } : {}),
    };
    const filter = combinedFilter(sourceId);
    const fields = Array.isArray(layer["popupFields"]) ? (layer["popupFields"] as string[]) : undefined;
    const handle = await mountSource(map, source, {
      sourceId: `honua-${id}`,
      layerId: id,
      ...(beforeId ? { beforeId } : {}),
      query: { ...baseQuery, ...(filter ? { filter } : {}) },
      maxGeoJsonFeatures: limit,
      ...(layer["renderer"] ? { renderer: rendererFrom(layer["renderer"]) } : {}),
      // (the same renderer object is kept on the mounted entry for legend items)
      ...(layer["paint"] ? { paint: layer["paint"] as never } : {}),
      ...(layer["geometry"] ? { geometry: layer["geometry"] as never } : {}),
      popup: { factory: () => new maplibregl.Popup({ maxWidth: "320px" }), ...(fields ? { fields } : {}), title: String(layer["title"] ?? id) },
      hover: true,
      fitBounds: layer["fitBounds"] === true,
    });
    const renderer = layer["renderer"] ? rendererFrom(layer["renderer"]) : undefined;
    mounted.set(id, { id, sourceId, title: String(layer["title"] ?? id), baseQuery, visible: true, handle, ...(renderer ? { renderer } : {}) });
    renderLayers();
    renderWidgets();
    const { strategy, featureCount, overflow, geometryKinds } = handle.diagnostics;
    return { id, strategy, featureCount, geometryKinds, ...(overflow ? { overflow } : {}), maplibreLayerIds: handle.layerIds };
  },

  async setFilter(id, clause) {
    if (clause) filters.set(id, clause);
    else filters.delete(id);
    const touched: Record<string, unknown> = {};
    for (const entry of mounted.values()) {
      const filter = combinedFilter(entry.sourceId);
      const diagnostics = await entry.handle.setFilter({ ...entry.baseQuery, ...(filter ? { filter } : {}) });
      touched[entry.id] = { featureCount: diagnostics.featureCount, overflow: diagnostics.overflow ?? null };
    }
    renderLayers();
    return { filterId: id, active: [...filters.keys()], layers: touched };
  },

  getSelection: () => selection,

  async selectFeature(target, options) {
    const qualified = typeof target === "object" && target !== null ? target : undefined;
    if (!qualified) throw new Error("selectFeature requires { sourceId, id }");
    const replace = options?.replace !== false;
    if (replace) {
      for (const previous of selection) {
        if (typeof previous !== "object") continue;
        for (const entry of mounted.values()) {
          if (entry.sourceId !== previous.sourceId) continue;
          map.removeFeatureState({ source: entry.handle.sourceId, id: previous.id }, "selected");
        }
      }
      selection = [];
    }
    let hits = 0;
    for (const entry of mounted.values()) {
      if (entry.sourceId !== qualified.sourceId) continue;
      map.setFeatureState({ source: entry.handle.sourceId, id: qualified.id }, { selected: true });
      hits += 1;
    }
    if (hits === 0) throw new Error(`No mounted layer is bound to source "${qualified.sourceId}"`);
    selection = [...selection, { sourceId: qualified.sourceId, id: qualified.id }];
    return selection;
  },

  async setLayerStyle(layerId: string, style: HonuaAgentLayerStyle) {
    const entry = mounted.get(layerId);
    if (!entry) throw new Error(`Unknown layer "${layerId}"`);
    const inline = (style.style ?? {}) as Record<string, unknown>;
    const applied: string[] = [];
    if (inline["renderer"] !== undefined) {
      const renderer = rendererFrom(inline["renderer"]);
      const diagnostics = await entry.handle.setRenderer(renderer);
      entry.renderer = renderer;
      applied.push(`renderer:${diagnostics.updates.at(-1)?.code ?? "applied"}`);
    }
    const paint = inline["paint"] as Record<string, Record<string, unknown>> | undefined;
    if (paint) {
      const suffix: Record<string, string> = { point: "-point", line: "-line", polygon: "-polygon", polygonOutline: "-polygon-outline" };
      for (const [kind, properties] of Object.entries(paint)) {
        const target = `${layerId}${suffix[kind] ?? ""}`;
        if (!entry.handle.layerIds.includes(target)) continue;
        for (const [name, value] of Object.entries(properties)) map.setPaintProperty(target, name as never, value as never);
        applied.push(`paint:${target}`);
      }
    }
    renderWidgets();
    if (applied.length === 0) {
      throw new Error('setLayerStyle expects style.renderer ({ kind: "class-breaks" | "unique-value" | "heatmap", … }) and/or style.paint ({ polygon: {...}, line: {...}, point: {...} })');
    }
    return { layerId, applied };
  },

  async setVisibility(args: SetVisibilityArgs) {
    const entry = mounted.get(args.layerId);
    if (!entry) throw new Error(`Unknown layer "${args.layerId}"`);
    for (const maplibreId of entry.handle.layerIds) {
      map.setLayoutProperty(maplibreId, "visibility", args.visible ? "visible" : "none");
    }
    entry.visible = args.visible;
    renderLayers();
    renderWidgets();
    return { layerId: args.layerId, visible: args.visible };
  },

  snapshot() {
    return {
      metadata: {
        widgets: [...widgets.values()].map((widget) => ({ id: widget.id, kind: widget.kind, title: widget.title })),
        availableWidgetKinds: ["layer-list", "legend"],
      },
    };
  },

  async addWidget(widget) {
    const docked = dockWidget({ id: widget.id, kind: widget.kind, ...(widget.title ? { title: widget.title } : {}) });
    return { id: docked.id, kind: docked.kind, title: docked.title, element: docked.element.tagName.toLowerCase() };
  },

  async removeWidget(widgetId) {
    const widget = widgets.get(widgetId);
    if (!widget) throw new Error(`Unknown widget "${widgetId}"`);
    widget.card.remove();
    widgets.delete(widgetId);
    return { id: widgetId, removed: true };
  },
};

// ---------------------------------------------------------------------------
// The AI map kit — provider-ready tool plane over the runtime adapter.
// ---------------------------------------------------------------------------

const kit = createHonuaAiMapKit({
  runtime,
  providerFormat: "mcp",
  policy: {
    actor: "claude-code",
    allowActions: true,
    onAudit: (event) => console.debug("[honua audit]", event),
  },
});

let seq = 0;

async function execute(call: HonuaAgentToolCall): Promise<HonuaAgentToolResult> {
  seq += 1;
  const started = performance.now();
  const result = await kit.execute(call);
  const ms = Math.round(performance.now() - started);
  const detail =
    result.status === "denied"
      ? result.deniedReason
      : result.status === "error"
        ? JSON.stringify(result.data ?? "").slice(0, 300)
        : result.degraded?.map((reason) => reason.message).join("; ");
  activity.push({
    seq,
    at: new Date().toISOString(),
    tool: call.name,
    args: (call as { args?: unknown }).args,
    status: result.status,
    ms,
    ...(detail ? { detail } : {}),
  });
  renderActivity();
  return result;
}

const ready = new Promise<void>((resolve) => {
  if (map.loaded()) resolve();
  else map.once("load", () => resolve());
});

declare global {
  interface Window {
    honua: {
      readonly map: maplibregl.Map;
      readonly kit: typeof kit;
      readonly runtime: HonuaAgentRuntime;
      readonly ready: Promise<void>;
      readonly activity: readonly ActivityEntry[];
      readonly catalog: readonly CatalogEntry[];
      execute(call: HonuaAgentToolCall): Promise<HonuaAgentToolResult>;
    };
  }
}

window.honua = { map, kit, runtime, ready, activity, catalog: CATALOG, execute };

void ready.then(() => {
  el("status").textContent = "Basemap ready. Waiting for the agent…";
  renderLayers();
});

// ---------------------------------------------------------------------------
// Agent link — the page pulls jobs from a loopback control channel so the
// agent can drive the map in whichever browser has this page open.
// ---------------------------------------------------------------------------

const CONTROL_URL: string = import.meta.env["VITE_CONTROL_URL"] ?? "http://127.0.0.1:47811";

function linkState(state: "connected" | "waiting", note?: string): void {
  const status = el("status");
  status.dataset["link"] = state;
  status.textContent =
    state === "connected"
      ? `Agent link live · ${mounted.size} layer(s) · ${activity.length} tool call(s)${note ? ` · ${note}` : ""}`
      : `Waiting for the agent control channel at ${CONTROL_URL}…`;
}

async function agentLink(): Promise<void> {
  await ready;
  for (;;) {
    try {
      const response = await fetch(`${CONTROL_URL}/next`, { cache: "no-store" });
      if (response.status === 204) {
        linkState("connected");
        continue;
      }
      const job = (await response.json()) as { id: string; kind: string; payload?: unknown };
      let output: unknown;
      let error: string | undefined;
      try {
        switch (job.kind) {
          case "call":
            output = await execute(job.payload as HonuaAgentToolCall);
            break;
          case "tools":
            output = kit.mcpTools;
            break;
          case "prompt":
            output = await kit.systemPrompt();
            break;
          case "context":
            output = await kit.context();
            break;
          case "viewport":
            output = viewport();
            break;
          case "say":
            say((job.payload as { role: string; text: string }).role, (job.payload as { role: string; text: string }).text);
            output = { ok: true };
            break;
          case "widgets":
            output = [...document.querySelectorAll<HTMLElement>(".widget-card")].map((card) => ({
              id: card.dataset["widget"],
              tag: card.querySelector("honua-layer-list, honua-legend")?.tagName.toLowerCase(),
              text: (card.querySelector("honua-layer-list, honua-legend")?.shadowRoot?.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 400),
            }));
            break;
          case "snapshot":
            output = { png: map.getCanvas().toDataURL("image/png"), viewport: viewport(), layers: runtime.listLayers?.(), activity: activity.slice(-5) };
            break;
          default:
            error = `unknown job kind "${job.kind}"`;
        }
      } catch (caught) {
        error = caught instanceof Error ? caught.message : String(caught);
      }
      await fetch(`${CONTROL_URL}/result`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: job.id, output, error }),
      });
      linkState("connected", `last: ${job.kind === "call" ? (job.payload as { name: string }).name : job.kind}`);
    } catch {
      linkState("waiting");
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
}

function say(role: string, text: string): void {
  const list = el("chat");
  const item = document.createElement("li");
  item.dataset["role"] = role;
  item.textContent = text;
  list.append(item);
  list.scrollTop = list.scrollHeight;
  if (role === "assistant" || role === "error") {
    (el("chat-send") as HTMLButtonElement).disabled = false;
  }
}

el("chat-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = el("chat-input") as HTMLTextAreaElement;
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  say("user", text);
  (el("chat-send") as HTMLButtonElement).disabled = true;
  try {
    const response = await fetch(`${CONTROL_URL}/chat`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!response.ok) say("error", `Agent refused the prompt: HTTP ${response.status}`);
  } catch (caught) {
    say("error", `Could not reach the agent: ${caught instanceof Error ? caught.message : String(caught)}`);
  }
});
(el("chat-input") as HTMLTextAreaElement).addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    el("chat-form").dispatchEvent(new Event("submit", { cancelable: true }));
  }
});

void agentLink();
