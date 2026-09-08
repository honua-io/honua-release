/**
 * Server compatibility contract parsing and evaluation. Decodes the
 * `GET /api/v1/admin/capabilities` envelope into the typed
 * {@link HonuaServerCompatibility} shape, and evaluates it against this
 * SDK's supported baseline (minimum server version, control-plane API
 * major/base-path, and release channel). Pure logic with no transport
 * coupling; the `HonuaClient` facade owns fetching + caching and delegates
 * the parse/evaluate steps here.
 *
 * @module
 */

import { HonuaHttpError } from "./errors.js";
import { trimTrailingSlashes } from "./path-utils.js";
import type { HonuaServerCompatibility } from "./types.js";

export const HONUA_MINIMUM_SUPPORTED_SERVER_VERSION = "1.0.0";
export const MINIMUM_SUPPORTED_SERVER_RELEASE_CHANNEL = "preview";

const SUPPORTED_CONTROL_PLANE_API_MAJOR = 1;
const SUPPORTED_CONTROL_PLANE_API_BASE_PATH = "/api/v1/admin";

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseCompatibilityEnvelope(payload: unknown): HonuaServerCompatibility {
  if (!isObject(payload)) {
    throw new TypeError("Server capabilities response must be a JSON object.");
  }

  if (payload.success === false) {
    const message = typeof payload.message === "string" ? payload.message : "Server capabilities request failed.";
    throw new Error(message);
  }

  if (!isObject(payload.data)) {
    throw new TypeError("Server capabilities response is missing a data object.");
  }

  return parseCompatibilityContract(payload.data.compatibility);
}

function parseCompatibilityContract(payload: unknown): HonuaServerCompatibility {
  if (!isObject(payload)) {
    throw new TypeError("Server capabilities response is missing data.compatibility.");
  }

  return {
    serverVersion: requireNonEmptyString(payload.serverVersion, "data.compatibility.serverVersion"),
    releaseChannel: requireNonEmptyString(payload.releaseChannel, "data.compatibility.releaseChannel"),
    controlPlaneApi: parseControlPlaneApi(payload.controlPlaneApi),
    metadataSchemas: parseMetadataSchemas(payload.metadataSchemas),
    features: parseCompatibilityFeatures(payload.features),
  };
}

function parseControlPlaneApi(payload: unknown): HonuaServerCompatibility["controlPlaneApi"] {
  if (!isObject(payload)) {
    throw new TypeError("Server capabilities response is missing data.compatibility.controlPlaneApi.");
  }

  return {
    major: requireInteger(payload.major, "data.compatibility.controlPlaneApi.major"),
    basePath: requireNonEmptyString(payload.basePath, "data.compatibility.controlPlaneApi.basePath"),
    deprecated: requireBoolean(payload.deprecated, "data.compatibility.controlPlaneApi.deprecated"),
  };
}

function parseMetadataSchemas(payload: unknown): HonuaServerCompatibility["metadataSchemas"] {
  if (!Array.isArray(payload)) {
    throw new TypeError("Server capabilities response is missing data.compatibility.metadataSchemas.");
  }

  return payload.map((entry, index) => {
    if (!isObject(entry)) {
      throw new TypeError(`Server capabilities response metadataSchemas[${index}] must be an object.`);
    }

    return {
      version: requireNonEmptyString(entry.version, `data.compatibility.metadataSchemas[${index}].version`),
      deprecated: requireBoolean(entry.deprecated, `data.compatibility.metadataSchemas[${index}].deprecated`),
    };
  });
}

function parseCompatibilityFeatures(payload: unknown): HonuaServerCompatibility["features"] {
  if (!isObject(payload)) {
    throw new TypeError("Server capabilities response is missing data.compatibility.features.");
  }

  return {
    metadataResources: requireBoolean(payload.metadataResources, "data.compatibility.features.metadataResources"),
    manifestExport: requireBoolean(payload.manifestExport, "data.compatibility.features.manifestExport"),
    manifestApply: requireBoolean(payload.manifestApply, "data.compatibility.features.manifestApply"),
    manifestDryRun: requireBoolean(payload.manifestDryRun, "data.compatibility.features.manifestDryRun"),
    manifestPrune: requireBoolean(payload.manifestPrune, "data.compatibility.features.manifestPrune"),
  };
}

function requireNonEmptyString(value: unknown, fieldName: string): string {
  if (typeof value !== "string") {
    throw new TypeError(`${fieldName} must be a string.`);
  }

  const trimmed = value.trim();
  if (trimmed.length === 0) {
    throw new TypeError(`${fieldName} must not be empty.`);
  }

  return trimmed;
}

function requireBoolean(value: unknown, fieldName: string): boolean {
  if (typeof value !== "boolean") {
    throw new TypeError(`${fieldName} must be a boolean.`);
  }
  return value;
}

function requireInteger(value: unknown, fieldName: string): number {
  if (typeof value !== "number" || !Number.isInteger(value)) {
    throw new TypeError(`${fieldName} must be an integer.`);
  }
  return value;
}

export function evaluateCompatibility(compatibility: HonuaServerCompatibility): string[] {
  const reasons: string[] = [];
  const minimumVersion = parseVersion(HONUA_MINIMUM_SUPPORTED_SERVER_VERSION);
  const serverVersion = parseVersion(compatibility.serverVersion);

  if (!minimumVersion) {
    reasons.push(
      `SDK minimum supported version '${HONUA_MINIMUM_SUPPORTED_SERVER_VERSION}' is not parseable for compatibility checks.`,
    );
    return reasons;
  }

  if (!serverVersion) {
    reasons.push(`Server version '${compatibility.serverVersion}' is not parseable for compatibility checks.`);
  } else if (compareVersions(serverVersion, minimumVersion) < 0) {
    reasons.push(
      `Server version ${compatibility.serverVersion} is older than the minimum supported ${HONUA_MINIMUM_SUPPORTED_SERVER_VERSION}.`,
    );
  }

  if (compatibility.controlPlaneApi.major !== SUPPORTED_CONTROL_PLANE_API_MAJOR) {
    reasons.push(
      `Control-plane API major ${compatibility.controlPlaneApi.major} is unsupported; expected ${SUPPORTED_CONTROL_PLANE_API_MAJOR}.`,
    );
  }

  if (normalizePathValue(compatibility.controlPlaneApi.basePath) !== SUPPORTED_CONTROL_PLANE_API_BASE_PATH) {
    reasons.push(
      `Control-plane API base path ${compatibility.controlPlaneApi.basePath} is unsupported; expected ${SUPPORTED_CONTROL_PLANE_API_BASE_PATH}.`,
    );
  }

  if (compatibility.controlPlaneApi.deprecated) {
    reasons.push(`Control-plane API major ${compatibility.controlPlaneApi.major} is marked deprecated by the server.`);
  }

  const actualReleaseChannelRank = getReleaseChannelRank(compatibility.releaseChannel);
  const minimumReleaseChannelRank = getReleaseChannelRank(MINIMUM_SUPPORTED_SERVER_RELEASE_CHANNEL) ?? 0;
  if (actualReleaseChannelRank === undefined) {
    reasons.push(`Server release channel '${compatibility.releaseChannel}' is not recognized by this SDK baseline.`);
  } else if (actualReleaseChannelRank < minimumReleaseChannelRank) {
    reasons.push(
      `Server release channel '${compatibility.releaseChannel}' is below the minimum supported '${MINIMUM_SUPPORTED_SERVER_RELEASE_CHANNEL}'.`,
    );
  }

  return reasons;
}

interface ParsedVersion {
  readonly numbers: readonly number[];
  readonly prerelease: readonly string[];
}

function parseVersion(version: string): ParsedVersion | undefined {
  const normalized = version.trim().replace(/^v/i, "");
  if (normalized.length === 0) {
    return undefined;
  }

  const coreAndPrerelease = normalized.split("+", 1)[0] ?? normalized;
  const [core, prerelease = ""] = coreAndPrerelease.split("-", 2);
  const numbers = core.split(".").map((segment) => Number.parseInt(segment, 10));
  if (numbers.length === 0 || numbers.some((segment) => !Number.isFinite(segment))) {
    return undefined;
  }

  const prereleaseParts =
    prerelease.length > 0
      ? prerelease
          .split(".")
          .map((segment) => segment.trim())
          .filter((segment) => segment.length > 0)
      : [];

  return {
    numbers,
    prerelease: prereleaseParts,
  };
}

function compareVersions(left: ParsedVersion, right: ParsedVersion): number {
  const length = Math.max(left.numbers.length, right.numbers.length);
  for (let index = 0; index < length; index += 1) {
    const leftPart = left.numbers[index] ?? 0;
    const rightPart = right.numbers[index] ?? 0;
    if (leftPart !== rightPart) {
      return leftPart < rightPart ? -1 : 1;
    }
  }

  if (left.prerelease.length === 0 && right.prerelease.length === 0) {
    return 0;
  }
  if (left.prerelease.length === 0) {
    return 1;
  }
  if (right.prerelease.length === 0) {
    return -1;
  }

  const prereleaseLength = Math.max(left.prerelease.length, right.prerelease.length);
  for (let index = 0; index < prereleaseLength; index += 1) {
    const leftPart = left.prerelease[index];
    const rightPart = right.prerelease[index];
    if (leftPart === undefined) {
      return -1;
    }
    if (rightPart === undefined) {
      return 1;
    }
    if (leftPart === rightPart) {
      continue;
    }

    const leftNumeric = Number.parseInt(leftPart, 10);
    const rightNumeric = Number.parseInt(rightPart, 10);
    const leftIsNumeric = String(leftNumeric) === leftPart;
    const rightIsNumeric = String(rightNumeric) === rightPart;

    if (leftIsNumeric && rightIsNumeric) {
      return leftNumeric < rightNumeric ? -1 : 1;
    }
    if (leftIsNumeric) {
      return -1;
    }
    if (rightIsNumeric) {
      return 1;
    }

    return leftPart < rightPart ? -1 : 1;
  }

  return 0;
}

export function describeCompatibilityError(error: unknown): string {
  if (error instanceof HonuaHttpError && error.statusCode === 404) {
    return "Server does not expose GET /api/v1/admin/capabilities.";
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

function normalizePathValue(path: string): string {
  const trimmed = path.trim();
  if (trimmed.length === 0) {
    return "";
  }

  const prefixed = trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
  return trimTrailingSlashes(prefixed);
}

function getReleaseChannelRank(releaseChannel: string): number | undefined {
  switch (releaseChannel.trim().toLowerCase()) {
    case "nightly":
      return 0;
    case "dev":
      return 1;
    case "alpha":
      return 2;
    case "preview":
      return 3;
    case "beta":
      return 4;
    case "rc":
      return 5;
    case "stable":
      return 6;
    case "lts":
      return 7;
    default:
      return undefined;
  }
}
