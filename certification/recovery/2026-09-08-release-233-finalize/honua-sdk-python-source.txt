"""Data models for the Honua Admin API."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields
from typing import Any, Literal, TypeAlias

MINIMUM_SUPPORTED_SERVER_VERSION = "1.0.0"
# Pre-GA Honua Server releases used CalVer; keep their established cutoff
# while accepting the GA SemVer identity above.
_MINIMUM_SUPPORTED_CALVER = "2026.3.0"
MINIMUM_SUPPORTED_CONTROL_PLANE_API_MAJOR = 1
MINIMUM_SUPPORTED_CONTROL_PLANE_BASE_PATH = "/api/v1/admin"
MINIMUM_SUPPORTED_SERVER_RELEASE_CHANNEL = "preview"

_RELEASE_CHANNEL_ORDER = {
    "nightly": 0,
    "dev": 1,
    "alpha": 2,
    "preview": 3,
    "beta": 4,
    "rc": 5,
    "stable": 6,
    "lts": 7,
}

_VERSION_COMPONENT_PATTERN = re.compile(r"\d+")
_CALVER_PATTERN = re.compile(r"^\d{4}[.-]\d{1,2}[.-]\d{1,2}(?:[-+].*)?$")


def _to_snake(name: str) -> str:
    """Convert camelCase to snake_case."""
    s1 = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()


def _to_camel(name: str) -> str:
    """Convert snake_case to camelCase."""
    parts = name.split("_")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _snake_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Convert top-level dict keys from camelCase to snake_case."""
    return {_to_snake(k): v for k, v in d.items()}


def _camel_keys(d: dict[str, Any]) -> dict[str, Any]:
    """Convert top-level dict keys from snake_case to camelCase."""
    return {_to_camel(k): v for k, v in d.items()}


def _extract_fields(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Extract only the fields that exist on *cls* from a snake-cased dict."""
    valid = {f.name for f in fields(cls)}
    return {k: v for k, v in data.items() if k in valid}


def _to_dict_value(value: Any) -> Any:
    """Convert nested SDK models to API dictionaries."""
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, list):
        return [_to_dict_value(item) for item in value]
    if isinstance(value, dict):
        return {k: _to_dict_value(v) for k, v in value.items()}
    return value


def _dataclass_to_camel_dict(instance: Any) -> dict[str, Any]:
    """Serialise dataclass fields to a camelCase API dictionary."""
    d: dict[str, Any] = {}
    for f in fields(instance):
        val = getattr(instance, f.name)
        if val is not None:
            d[_to_camel(f.name)] = _to_dict_value(val)
    return d


def _model_list(cls: Any, values: Any) -> list[Any]:
    """Parse a list of nested dict payloads into model instances."""
    if not isinstance(values, list):
        return []
    return [cls.from_dict(item) if isinstance(item, dict) else item for item in values]


def _parse_version_components(version: str | None) -> tuple[int, int, int] | None:
    """Parse a coarse version tuple from semver- or calver-like version strings."""
    if not version:
        return None
    parts = [int(part) for part in _VERSION_COMPONENT_PATTERN.findall(version)]
    if len(parts) < 3:
        return None
    return (parts[0], parts[1], parts[2])


def _is_calver(version: str | None) -> bool:
    return bool(version and _CALVER_PATTERN.fullmatch(version))


# ---------------------------------------------------------------------------
# Response models (frozen)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdminServiceSummary:
    """Admin-API view of a published service (name + protocol roll-up).

    Distinct from :class:`honua_sdk.models.ServiceSummary` (which is the
    data-plane catalog summary). Renamed from the original
    ``ServiceSummary`` to remove the cross-package name collision; the old
    name is kept as a module-level alias below for back-compat.
    """

    service_name: str
    description: str | None
    layer_count: int
    enabled_protocols: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminServiceSummary:
        d = _snake_keys(data)
        d.setdefault("enabled_protocols", [])
        return cls(**_extract_fields(cls, d))


# Deprecated alias preserved for back-compat with callers (and
# honua_admin._client / _async_client) that still import ``ServiceSummary``
# from this module. Prefer :class:`AdminServiceSummary` in new code.
ServiceSummary = AdminServiceSummary


@dataclass(frozen=True, slots=True)
class AccessPolicyResponse:
    allow_anonymous: bool
    allow_anonymous_write: bool
    allowed_roles: list[str]
    allowed_write_roles: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AccessPolicyResponse:
        d = _snake_keys(data)
        d.setdefault("allowed_roles", [])
        d.setdefault("allowed_write_roles", [])
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class TimeInfoResponse:
    start_time_field: str | None
    end_time_field: str | None
    track_id_field: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TimeInfoResponse:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class MapServerSettings:
    max_image_width: int
    max_image_height: int
    default_image_width: int
    default_image_height: int
    default_dpi: int
    default_format: str
    default_transparent: bool
    max_features_per_layer: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MapServerSettings:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ServiceSettingsResponse:
    service_name: str
    enabled_protocols: list[str]
    available_protocols: list[str]
    access_policy: AccessPolicyResponse | None
    time_info: TimeInfoResponse | None
    map_server: MapServerSettings | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ServiceSettingsResponse:
        d = _snake_keys(data)
        d.setdefault("enabled_protocols", [])
        d.setdefault("available_protocols", [])
        ap = d.get("access_policy")
        ti = d.get("time_info")
        ms = d.get("map_server")
        d["access_policy"] = AccessPolicyResponse.from_dict(ap) if isinstance(ap, dict) else None
        d["time_info"] = TimeInfoResponse.from_dict(ti) if isinstance(ti, dict) else None
        d["map_server"] = MapServerSettings.from_dict(ms) if isinstance(ms, dict) else None
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class SecureConnectionSummary:
    connection_id: str
    name: str
    description: str | None
    host: str
    port: int
    database_name: str
    username: str
    ssl_required: bool
    ssl_mode: str | None
    storage_type: str | None
    is_active: bool
    health_status: str | None
    last_health_check: str | None
    created_at: str | None
    created_by: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecureConnectionSummary:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class SecureConnectionDetail:
    connection_id: str
    name: str
    description: str | None
    host: str
    port: int
    database_name: str
    username: str
    ssl_required: bool
    ssl_mode: str | None
    storage_type: str | None
    is_active: bool
    health_status: str | None
    last_health_check: str | None
    created_at: str | None
    created_by: str | None
    credential_reference: str | None
    encryption_version: int | None
    updated_at: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SecureConnectionDetail:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    connection_id: str | None
    connection_name: str | None
    is_healthy: bool
    tested_at: str | None
    message: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ConnectionTestResult:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class EncryptionValidationResult:
    is_valid: bool
    current_key_version: int | None
    validated_at: str | None
    message: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EncryptionValidationResult:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class KeyRotationResult:
    previous_key_version: int | None
    new_key_version: int | None
    rotated_at: str | None
    message: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> KeyRotationResult:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ResourceMetadata:
    id: str | None
    name: str
    namespace: str
    labels: dict[str, str]
    annotations: dict[str, str]
    resource_version: str | None
    generation: int | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResourceMetadata:
        d = _snake_keys(data)
        d.setdefault("id", None)
        d.setdefault("labels", {})
        d.setdefault("annotations", {})
        d.setdefault("resource_version", None)
        d.setdefault("generation", None)
        d.setdefault("created_at", None)
        d.setdefault("updated_at", None)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class MetadataResource:
    api_version: str
    kind: str
    metadata: ResourceMetadata
    spec: dict[str, Any]
    status: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetadataResource:
        d = _snake_keys(data)
        md = d.get("metadata")
        d["metadata"] = ResourceMetadata.from_dict(md) if isinstance(md, dict) else md
        d.setdefault("spec", {})
        d.setdefault("status", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        """Serialise back to camelCase dict for API requests."""
        md = self.metadata
        meta_dict: dict[str, Any] = {
            "name": md.name,
            "namespace": md.namespace,
            "labels": md.labels,
            "annotations": md.annotations,
        }
        if md.id is not None:
            meta_dict["id"] = md.id
        if md.resource_version is not None:
            meta_dict["resourceVersion"] = md.resource_version
        if md.generation is not None:
            meta_dict["generation"] = md.generation
        if md.created_at is not None:
            meta_dict["createdAt"] = md.created_at
        if md.updated_at is not None:
            meta_dict["updatedAt"] = md.updated_at

        result: dict[str, Any] = {
            "apiVersion": self.api_version,
            "kind": self.kind,
            "metadata": meta_dict,
            "spec": self.spec,
        }
        if self.status is not None:
            result["status"] = self.status
        return result


@dataclass(frozen=True, slots=True)
class MetadataResourceIdentifier:
    kind: str
    namespace: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetadataResourceIdentifier:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ManifestApplySummary:
    created: int
    updated: int
    deleted: int
    skipped: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestApplySummary:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ManifestApplyEntry:
    action: str
    resource: MetadataResourceIdentifier
    message: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestApplyEntry:
        d = _snake_keys(data)
        res = d.get("resource")
        d["resource"] = MetadataResourceIdentifier.from_dict(res) if isinstance(res, dict) else res
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class ManifestApplyResult:
    dry_run: bool
    summary: ManifestApplySummary
    entries: list[ManifestApplyEntry]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ManifestApplyResult:
        d = _snake_keys(data)
        s = d.get("summary")
        d["summary"] = ManifestApplySummary.from_dict(s) if isinstance(s, dict) else s
        raw_entries = d.get("entries", [])
        d["entries"] = [ManifestApplyEntry.from_dict(e) for e in raw_entries]
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class MetadataManifest:
    api_version: str
    generated_at: str | None
    resources: list[MetadataResource]
    drifted_resources: list[MetadataResourceIdentifier]
    manifest_hash: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MetadataManifest:
        d = _snake_keys(data)
        raw_res = d.get("resources", [])
        d["resources"] = [MetadataResource.from_dict(r) for r in raw_res]
        raw_drifted = d.get("drifted_resources", [])
        d["drifted_resources"] = [MetadataResourceIdentifier.from_dict(r) for r in raw_drifted]
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminVersionResponse:
    version: str
    metadata_api_version: str
    server_time: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminVersionResponse:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminCapabilitiesResponse:
    metadata_api_versions: list[str] = field(default_factory=list)
    resource_kinds: list[str] = field(default_factory=list)
    manifest_supported: bool = False
    manifest_dry_run_supported: bool = False
    manifest_prune_supported: bool = False
    compatibility: AdminCompatibilityMetadata | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminCapabilitiesResponse:
        d = _snake_keys(data)
        d.setdefault("metadata_api_versions", [])
        d.setdefault("resource_kinds", [])
        d.setdefault("manifest_supported", False)
        d.setdefault("manifest_dry_run_supported", False)
        d.setdefault("manifest_prune_supported", False)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            AdminCompatibilityMetadata.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else None
        )
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminControlPlaneApiCompatibility:
    major: int = 0
    base_path: str = ""
    deprecated: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminControlPlaneApiCompatibility:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminMetadataSchemaCompatibility:
    version: str
    deprecated: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminMetadataSchemaCompatibility:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminCompatibilityFeatureFlags:
    metadata_resources: bool = False
    manifest_export: bool = False
    manifest_apply: bool = False
    manifest_dry_run: bool = False
    manifest_prune: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminCompatibilityFeatureFlags:
        d = _snake_keys(data)
        d.setdefault("metadata_resources", False)
        d.setdefault("manifest_export", False)
        d.setdefault("manifest_apply", False)
        d.setdefault("manifest_dry_run", False)
        d.setdefault("manifest_prune", False)
        return cls(**_extract_fields(cls, d))

    def supports(self, feature: str) -> bool:
        return bool(getattr(self, _to_snake(feature).replace("-", "_"), False))


@dataclass(frozen=True, slots=True)
class AdminCompatibilityMetadata:
    server_version: str = ""
    release_channel: str = ""
    control_plane_api: AdminControlPlaneApiCompatibility = field(
        default_factory=AdminControlPlaneApiCompatibility
    )
    metadata_schemas: list[AdminMetadataSchemaCompatibility] = field(default_factory=list)
    features: AdminCompatibilityFeatureFlags = field(default_factory=AdminCompatibilityFeatureFlags)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AdminCompatibilityMetadata:
        d = _snake_keys(data)
        control_plane_api = d.get("control_plane_api")
        metadata_schemas = d.get("metadata_schemas", [])
        features = d.get("features")
        d["control_plane_api"] = (
            AdminControlPlaneApiCompatibility.from_dict(control_plane_api)
            if isinstance(control_plane_api, dict)
            else AdminControlPlaneApiCompatibility(
                major=0,
                base_path="",
                deprecated=False,
            )
        )
        d["metadata_schemas"] = [
            AdminMetadataSchemaCompatibility.from_dict(item)
            for item in metadata_schemas
            if isinstance(item, dict)
        ]
        d["features"] = (
            AdminCompatibilityFeatureFlags.from_dict(features)
            if isinstance(features, dict)
            else AdminCompatibilityFeatureFlags(
                metadata_resources=False,
                manifest_export=False,
                manifest_apply=False,
                manifest_dry_run=False,
                manifest_prune=False,
            )
        )
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class AdminCompatibilityBaseline:
    minimum_server_version: str = MINIMUM_SUPPORTED_SERVER_VERSION
    control_plane_api_major: int = MINIMUM_SUPPORTED_CONTROL_PLANE_API_MAJOR
    base_path: str = MINIMUM_SUPPORTED_CONTROL_PLANE_BASE_PATH
    minimum_release_channel: str = MINIMUM_SUPPORTED_SERVER_RELEASE_CHANNEL


@dataclass(frozen=True, slots=True)
class AdminCompatibilityCheckResult:
    supported: bool
    baseline: AdminCompatibilityBaseline
    compatibility: AdminCompatibilityMetadata | None
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def get_release_channel_rank(channel: str | None) -> int | None:
    if channel is None:
        return None
    return _RELEASE_CHANNEL_ORDER.get(channel.lower())


def evaluate_admin_compatibility(
    compatibility: AdminCompatibilityMetadata | None,
    baseline: AdminCompatibilityBaseline | None = None,
) -> AdminCompatibilityCheckResult:
    """Evaluate a server compatibility contract against the SDK baseline."""
    baseline = baseline or AdminCompatibilityBaseline()
    reasons: list[str] = []
    warnings: list[str] = []

    if compatibility is None:
        reasons.append("Server did not return a compatibility contract.")
        return AdminCompatibilityCheckResult(
            supported=False,
            baseline=baseline,
            compatibility=None,
            reasons=reasons,
            warnings=warnings,
        )

    actual_version = _parse_version_components(compatibility.server_version)
    minimum_version_text = (
        _MINIMUM_SUPPORTED_CALVER
        if _is_calver(compatibility.server_version)
        else baseline.minimum_server_version
    )
    minimum_version = _parse_version_components(minimum_version_text)
    if actual_version is None:
        reasons.append(f"Server version {compatibility.server_version!r} could not be parsed.")
    elif minimum_version is None:
        reasons.append("SDK minimum supported server version baseline could not be parsed.")
    elif actual_version < minimum_version:
        reasons.append(
            "Server version "
            f"{compatibility.server_version!r} is below required "
            f"{minimum_version_text!r}."
        )

    if compatibility.control_plane_api.major != baseline.control_plane_api_major:
        reasons.append(
            "Server control-plane API major "
            f"{compatibility.control_plane_api.major} does not match required "
            f"{baseline.control_plane_api_major}."
        )

    if compatibility.control_plane_api.base_path != baseline.base_path:
        reasons.append(
            "Server control-plane base path "
            f"{compatibility.control_plane_api.base_path!r} does not match "
            f"required {baseline.base_path!r}."
        )

    if compatibility.control_plane_api.deprecated:
        warnings.append("Server control-plane API major is marked deprecated.")

    actual_rank = get_release_channel_rank(compatibility.release_channel)
    minimum_rank = get_release_channel_rank(baseline.minimum_release_channel)
    if actual_rank is None:
        reasons.append(f"Server release channel {compatibility.release_channel!r} is unknown.")
    elif minimum_rank is None or actual_rank < minimum_rank:
        reasons.append(
            "Server release channel "
            f"{compatibility.release_channel!r} is below required "
            f"{baseline.minimum_release_channel!r}."
        )

    return AdminCompatibilityCheckResult(
        supported=not reasons,
        baseline=baseline,
        compatibility=compatibility,
        reasons=reasons,
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    name: str
    data_type: str
    is_nullable: bool
    is_primary_key: bool
    max_length: int | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ColumnInfo:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class TableInfo:
    schema: str
    table: str
    geometry_column: str | None
    geometry_type: str | None
    srid: int | None
    estimated_rows: int | None
    columns: list[ColumnInfo]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableInfo:
        d = _snake_keys(data)
        raw_cols = d.get("columns", [])
        d["columns"] = [ColumnInfo.from_dict(c) for c in raw_cols]
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class TableDiscoveryResponse:
    tables: list[TableInfo]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableDiscoveryResponse:
        d = _snake_keys(data)
        raw_tables = d.get("tables", [])
        d["tables"] = [TableInfo.from_dict(t) for t in raw_tables]
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class PublishedLayerSummary:
    layer_id: int
    layer_name: str
    schema: str
    table: str
    description: str | None
    geometry_type: str | None
    srid: int | None
    primary_key: str | None
    field_count: int
    enabled: bool
    service_name: str | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PublishedLayerSummary:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class LayerStyleResponse:
    map_libre_style: dict[str, Any] | None
    drawing_info: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LayerStyleResponse:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


# ---------------------------------------------------------------------------
# OGC API - Styles (styleId-keyed; ADR-0048)
# ---------------------------------------------------------------------------

# Stylesheet encoding identifiers accepted by the styleId-keyed client. These
# map to the ``Accept`` media types the OGC API - Styles surface negotiates on
# (``GET /ogc/styles/{styleId}``). ``mapbox-style`` (MapLibre/Mapbox JSON) is
# canonical; ``sld-1.0`` / ``sld-1.1`` are derived on demand by the server.
StyleEncoding: "TypeAlias" = Literal["mapbox-style", "sld-1.0", "sld-1.1"]

STYLE_ENCODING_MEDIA_TYPES: dict[str, str] = {
    "mapbox-style": "application/vnd.mapbox.style+json",
    "sld-1.0": "application/vnd.ogc.sld+xml;version=1.0",
    "sld-1.1": "application/vnd.ogc.sld+xml;version=1.1",
}

DEFAULT_STYLE_ENCODING: "StyleEncoding" = "mapbox-style"


def style_encoding_media_type(encoding: "StyleEncoding") -> str:
    """Return the ``Accept`` media type for a stylesheet *encoding*.

    Raises:
        ValueError: ``encoding`` is not one of ``mapbox-style``,
            ``sld-1.0``, or ``sld-1.1``.
    """
    try:
        return STYLE_ENCODING_MEDIA_TYPES[encoding]
    except KeyError as exc:  # pragma: no cover - guarded by Literal typing
        valid = ", ".join(sorted(STYLE_ENCODING_MEDIA_TYPES))
        raise ValueError(f"Unsupported style encoding {encoding!r}; expected one of {valid}.") from exc


@dataclass(frozen=True, slots=True)
class OgcStyleLink:
    """A single OGC link (``rel`` / ``type`` / ``href`` / ``title``)."""

    href: str
    rel: str | None = None
    type: str | None = None
    title: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OgcStyleLink:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class OgcStyleSummary:
    """An entry in the OGC API - Styles styles list, keyed by ``style_id``."""

    style_id: str
    title: str | None = None
    links: list[OgcStyleLink] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OgcStyleSummary:
        d = _snake_keys(data)
        # The wire shape keys the identifier as ``id``; expose it as ``style_id``.
        if "id" in d and "style_id" not in d:
            d["style_id"] = d["id"]
        d["links"] = _model_list(OgcStyleLink, d.get("links", []))
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class OgcStylesList:
    """Response for ``GET /ogc/styles``: the styles list plus optional default."""

    styles: list[OgcStyleSummary] = field(default_factory=list)
    default: str | None = None
    links: list[OgcStyleLink] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OgcStylesList:
        d = _snake_keys(data)
        d["styles"] = _model_list(OgcStyleSummary, d.get("styles", []))
        d["links"] = _model_list(OgcStyleLink, d.get("links", []))
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class OgcStyleMetadata:
    """Response for ``GET /ogc/styles/{styleId}/metadata``."""

    style_id: str
    title: str | None = None
    description: str | None = None
    keywords: list[str] = field(default_factory=list)
    license: str | None = None
    version: str | None = None
    links: list[OgcStyleLink] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OgcStyleMetadata:
        d = _snake_keys(data)
        if "id" in d and "style_id" not in d:
            d["style_id"] = d["id"]
        if not isinstance(d.get("keywords"), list):
            d["keywords"] = []
        d["links"] = _model_list(OgcStyleLink, d.get("links", []))
        return cls(**_extract_fields(cls, d))


@dataclass(frozen=True, slots=True)
class OgcStylesheet:
    """A content-negotiated stylesheet returned by ``GET /ogc/styles/{styleId}``.

    ``encoding`` is the requested :data:`StyleEncoding`; ``media_type`` is the
    ``Content-Type`` the server returned. For ``mapbox-style`` the parsed
    MapLibre/Mapbox document is available via :meth:`as_json`; SLD encodings
    are returned as raw XML text in ``content``.
    """

    style_id: str
    encoding: "StyleEncoding"
    media_type: str
    content: str

    def as_json(self) -> dict[str, Any]:
        """Parse ``content`` as a JSON object (MapLibre/Mapbox stylesheet).

        Raises:
            ValueError: ``content`` is not a JSON object (e.g. an SLD payload).
        """
        parsed = json.loads(self.content)
        if not isinstance(parsed, dict):
            raise ValueError("Stylesheet content is not a JSON object.")
        return parsed


# ---------------------------------------------------------------------------
# Request models (mutable)
# ---------------------------------------------------------------------------


@dataclass
class CreateSecureConnectionRequest:
    name: str
    description: str | None = None
    host: str = ""
    port: int = 5432
    database_name: str = ""
    username: str = ""
    password: str | None = field(default=None, repr=False)
    secret_reference: str | None = None
    secret_type: str | None = None
    ssl_required: bool = False
    ssl_mode: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is not None:
                d[_to_camel(f.name)] = val
        return d


@dataclass
class UpdateSecureConnectionRequest:
    description: str | None = None
    host: str | None = None
    port: int | None = None
    database_name: str | None = None
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    ssl_required: bool | None = None
    ssl_mode: str | None = None
    is_active: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is not None:
                d[_to_camel(f.name)] = val
        return d


@dataclass
class PublishLayerRequest:
    schema: str = "public"
    table: str = ""
    layer_name: str | None = None
    description: str | None = None
    geometry_column: str | None = None
    geometry_type: str | None = None
    srid: int | None = None
    primary_key: str | None = None
    fields_list: list[str] | None = None
    service_name: str | None = None
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if val is not None:
                key = f.name
                # Map fields_list back to "fields" in the API
                if key == "fields_list":
                    key = "fields"
                d[_to_camel(key)] = val
        return d


@dataclass
class ManifestApplyRequest:
    resources: list[MetadataResource] = field(default_factory=list)
    dry_run: bool = False
    prune: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": [r.to_dict() for r in self.resources],
            "dryRun": self.dry_run,
            "prune": self.prune,
        }


@dataclass
class LayerStyleUpdateRequest:
    map_libre_style: dict[str, Any] | None = None
    drawing_info: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.map_libre_style is not None:
            d["mapLibreStyle"] = self.map_libre_style
        if self.drawing_info is not None:
            d["drawingInfo"] = self.drawing_info
        return d


# ---------------------------------------------------------------------------
# Migration toolkit artifact models
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MigrationSourceIdentity:
    display_name: str
    base_url: str
    product: str | None = None
    version: str | None = None
    build: str | None = None
    service_type: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationSourceIdentity:
        d = _snake_keys(data)
        d.setdefault("product", None)
        d.setdefault("version", None)
        d.setdefault("build", None)
        d.setdefault("service_type", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryAuthPosture:
    mode: str
    credentials_supplied: bool = False
    access_confirmed: bool = False
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryAuthPosture:
        d = _snake_keys(data)
        d.setdefault("credentials_supplied", False)
        d.setdefault("access_confirmed", False)
        d.setdefault("notes", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryCompleteness:
    status: str
    warnings: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryCompleteness:
        d = _snake_keys(data)
        d.setdefault("warnings", [])
        d.setdefault("missing_artifacts", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventorySummary:
    container_count: int = 0
    resource_count: int = 0
    style_count: int = 0
    external_dependency_count: int = 0
    compatible_count: int = 0
    partially_compatible_count: int = 0
    incompatible_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventorySummary:
        d = _snake_keys(data)
        d.setdefault("container_count", 0)
        d.setdefault("resource_count", 0)
        d.setdefault("style_count", 0)
        d.setdefault("external_dependency_count", 0)
        d.setdefault("compatible_count", 0)
        d.setdefault("partially_compatible_count", 0)
        d.setdefault("incompatible_count", 0)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationCompatibilityAssessment:
    level: str
    reason: str
    code: str | None = None
    warnings: list[str] = field(default_factory=list)
    manual_steps: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationCompatibilityAssessment:
        d = _snake_keys(data)
        d.setdefault("code", None)
        d.setdefault("warnings", [])
        d.setdefault("manual_steps", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryCodedValue:
    code: str
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryCodedValue:
        d = _snake_keys(data)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationSpatialReferenceInfo:
    role: str
    source_value: str | None = None
    srid: int | None = None
    crs_uri: str | None = None
    datum: str | None = None
    unit: str | None = None
    axis_order: str | None = None
    is_geographic: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationSpatialReferenceInfo:
        d = _snake_keys(data)
        d.setdefault("source_value", None)
        d.setdefault("srid", None)
        d.setdefault("crs_uri", None)
        d.setdefault("datum", None)
        d.setdefault("unit", None)
        d.setdefault("axis_order", None)
        d.setdefault("is_geographic", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryField:
    name: str
    field_type: str
    alias: str | None = None
    nullable: bool | None = None
    domain_type: str | None = None
    domain_name: str | None = None
    domain_values: list[MigrationInventoryCodedValue] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryField:
        d = _snake_keys(data)
        d.setdefault("alias", None)
        d.setdefault("nullable", None)
        d.setdefault("domain_type", None)
        d.setdefault("domain_name", None)
        domain_values = d.get("domain_values")
        d["domain_values"] = (
            _model_list(MigrationInventoryCodedValue, domain_values)
            if domain_values is not None
            else None
        )
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryContainer:
    id: str
    kind: str
    name: str
    compatibility: MigrationCompatibilityAssessment
    title: str | None = None
    description: str | None = None
    is_default: bool | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryContainer:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("title", None)
        d.setdefault("description", None)
        d.setdefault("is_default", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryResource:
    id: str
    container_id: str
    kind: str
    name: str
    compatibility: MigrationCompatibilityAssessment
    title: str | None = None
    description: str | None = None
    geometry_type: str | None = None
    feature_count: int | None = None
    has_attachments: bool | None = None
    capabilities: list[str] = field(default_factory=list)
    spatial_references: list[MigrationSpatialReferenceInfo] = field(default_factory=list)
    fields: list[MigrationInventoryField] = field(default_factory=list)
    style_ids: list[str] = field(default_factory=list)
    external_dependency_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryResource:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("title", None)
        d.setdefault("description", None)
        d.setdefault("geometry_type", None)
        d.setdefault("feature_count", None)
        d.setdefault("has_attachments", None)
        d.setdefault("capabilities", [])
        d["spatial_references"] = _model_list(MigrationSpatialReferenceInfo, d.get("spatial_references", []))
        d["fields"] = _model_list(MigrationInventoryField, d.get("fields", []))
        d.setdefault("style_ids", [])
        d.setdefault("external_dependency_ids", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationInventoryStyle:
    id: str
    container_id: str
    kind: str
    name: str
    compatibility: MigrationCompatibilityAssessment
    format: str | None = None
    resource_ids: list[str] = field(default_factory=list)
    external_dependency_ids: list[str] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationInventoryStyle:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("format", None)
        d.setdefault("resource_ids", [])
        d.setdefault("external_dependency_ids", [])
        d.setdefault("metadata", {})
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationExternalDependency:
    id: str
    container_id: str
    kind: str
    name: str
    compatibility: MigrationCompatibilityAssessment
    resource_id: str | None = None
    dependency_type: str | None = None
    address: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    spatial_references: list[MigrationSpatialReferenceInfo] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationExternalDependency:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("resource_id", None)
        d.setdefault("dependency_type", None)
        d.setdefault("address", None)
        d.setdefault("metadata", {})
        d["spatial_references"] = _model_list(MigrationSpatialReferenceInfo, d.get("spatial_references", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationSourceInventoryArtifact:
    source_kind: str
    source: MigrationSourceIdentity
    auth_posture: MigrationInventoryAuthPosture
    scan_completeness: MigrationInventoryCompleteness
    summary: MigrationInventorySummary
    overall_compatibility: MigrationCompatibilityAssessment
    containers: list[MigrationInventoryContainer] = field(default_factory=list)
    resources: list[MigrationInventoryResource] = field(default_factory=list)
    styles: list[MigrationInventoryStyle] = field(default_factory=list)
    external_dependencies: list[MigrationExternalDependency] = field(default_factory=list)
    artifact_kind: str = "honua.migration.source-inventory"
    artifact_version: str = "1.0"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationSourceInventoryArtifact:
        d = _snake_keys(data)
        source = d.get("source")
        auth_posture = d.get("auth_posture")
        scan_completeness = d.get("scan_completeness")
        summary = d.get("summary")
        compatibility = d.get("overall_compatibility")
        d["source"] = MigrationSourceIdentity.from_dict(source) if isinstance(source, dict) else source
        d["auth_posture"] = (
            MigrationInventoryAuthPosture.from_dict(auth_posture)
            if isinstance(auth_posture, dict)
            else auth_posture
        )
        d["scan_completeness"] = (
            MigrationInventoryCompleteness.from_dict(scan_completeness)
            if isinstance(scan_completeness, dict)
            else scan_completeness
        )
        d["summary"] = MigrationInventorySummary.from_dict(summary) if isinstance(summary, dict) else summary
        d["overall_compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d["containers"] = _model_list(MigrationInventoryContainer, d.get("containers", []))
        d["resources"] = _model_list(MigrationInventoryResource, d.get("resources", []))
        d["styles"] = _model_list(MigrationInventoryStyle, d.get("styles", []))
        d["external_dependencies"] = _model_list(MigrationExternalDependency, d.get("external_dependencies", []))
        d.setdefault("artifact_kind", "honua.migration.source-inventory")
        d.setdefault("artifact_version", "1.0")
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_camel_dict(self)
        return {
            "artifactKind": d.pop("artifactKind"),
            "artifactVersion": d.pop("artifactVersion"),
            **d,
        }


@dataclass(frozen=True, slots=True)
class MigrationManifestSummary:
    source_resource_count: int = 0
    target_resource_count: int = 0
    style_action_count: int = 0
    manual_review_count: int = 0
    unsupported_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationManifestSummary:
        d = _snake_keys(data)
        d.setdefault("source_resource_count", 0)
        d.setdefault("target_resource_count", 0)
        d.setdefault("style_action_count", 0)
        d.setdefault("manual_review_count", 0)
        d.setdefault("unsupported_count", 0)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationManifestTargetResource:
    source_resource_id: str
    source_kind: str
    action: str
    target_service_name: str
    target_resource_name: str
    compatibility: MigrationCompatibilityAssessment
    geometry_type: str | None = None
    fields: list[MigrationInventoryField] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    spatial_references: list[MigrationSpatialReferenceInfo] = field(default_factory=list)
    style_ids: list[str] = field(default_factory=list)
    external_dependency_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationManifestTargetResource:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("geometry_type", None)
        d["fields"] = _model_list(MigrationInventoryField, d.get("fields", []))
        d.setdefault("capabilities", [])
        d["spatial_references"] = _model_list(MigrationSpatialReferenceInfo, d.get("spatial_references", []))
        d.setdefault("style_ids", [])
        d.setdefault("external_dependency_ids", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationManifestStyleAction:
    source_style_id: str
    action: str
    compatibility: MigrationCompatibilityAssessment
    format: str | None = None
    resource_ids: list[str] = field(default_factory=list)
    external_dependency_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationManifestStyleAction:
        d = _snake_keys(data)
        compatibility = d.get("compatibility")
        d["compatibility"] = (
            MigrationCompatibilityAssessment.from_dict(compatibility)
            if isinstance(compatibility, dict)
            else compatibility
        )
        d.setdefault("format", None)
        d.setdefault("resource_ids", [])
        d.setdefault("external_dependency_ids", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationManifestReviewItem:
    source_id: str
    kind: str
    code: str
    severity: str
    reason: str
    manual_steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationManifestReviewItem:
        d = _snake_keys(data)
        d.setdefault("manual_steps", [])
        d.setdefault("warnings", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationManifestArtifact:
    source_kind: str
    source: MigrationSourceIdentity
    summary: MigrationManifestSummary
    target_resources: list[MigrationManifestTargetResource] = field(default_factory=list)
    style_actions: list[MigrationManifestStyleAction] = field(default_factory=list)
    manual_review_items: list[MigrationManifestReviewItem] = field(default_factory=list)
    unsupported_items: list[MigrationManifestReviewItem] = field(default_factory=list)
    artifact_kind: str = "honua.migration.manifest"
    artifact_version: str = "1.0"
    source_artifact_kind: str = "honua.migration.source-inventory"
    source_artifact_version: str = "1.0"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationManifestArtifact:
        d = _snake_keys(data)
        source = d.get("source")
        summary = d.get("summary")
        d["source"] = MigrationSourceIdentity.from_dict(source) if isinstance(source, dict) else source
        d["summary"] = MigrationManifestSummary.from_dict(summary) if isinstance(summary, dict) else summary
        d["target_resources"] = _model_list(MigrationManifestTargetResource, d.get("target_resources", []))
        d["style_actions"] = _model_list(MigrationManifestStyleAction, d.get("style_actions", []))
        d["manual_review_items"] = _model_list(MigrationManifestReviewItem, d.get("manual_review_items", []))
        d["unsupported_items"] = _model_list(MigrationManifestReviewItem, d.get("unsupported_items", []))
        d.setdefault("artifact_kind", "honua.migration.manifest")
        d.setdefault("artifact_version", "1.0")
        d.setdefault("source_artifact_kind", "honua.migration.source-inventory")
        d.setdefault("source_artifact_version", "1.0")
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_camel_dict(self)
        return {
            "artifactKind": d.pop("artifactKind"),
            "artifactVersion": d.pop("artifactVersion"),
            "sourceArtifactKind": d.pop("sourceArtifactKind"),
            "sourceArtifactVersion": d.pop("sourceArtifactVersion"),
            **d,
        }


@dataclass(frozen=True, slots=True)
class MigrationParityEvidenceItem:
    id: str
    state: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    related_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationParityEvidenceItem:
        d = _snake_keys(data)
        d.setdefault("evidence", [])
        d.setdefault("remediation", [])
        d.setdefault("related_ids", [])
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationParityEvidenceSection:
    id: str
    title: str
    state: str
    items: list[MigrationParityEvidenceItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationParityEvidenceSection:
        d = _snake_keys(data)
        d["items"] = _model_list(MigrationParityEvidenceItem, d.get("items", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationCutoverReadinessItem:
    id: str
    title: str
    state: str
    evidence: list[str] = field(default_factory=list)
    remediation: list[str] = field(default_factory=list)
    owner: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationCutoverReadinessItem:
        d = _snake_keys(data)
        d.setdefault("evidence", [])
        d.setdefault("remediation", [])
        d.setdefault("owner", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationCutoverReadinessSummary:
    state: str
    items: list[MigrationCutoverReadinessItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationCutoverReadinessSummary:
        d = _snake_keys(data)
        d["items"] = _model_list(MigrationCutoverReadinessItem, d.get("items", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationReadinessAttestationItem:
    id: str
    state: str
    evidence: list[str] = field(default_factory=list)
    owner: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationReadinessAttestationItem:
        d = _snake_keys(data)
        d.setdefault("evidence", [])
        d.setdefault("owner", None)
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationReadinessAttestation:
    items: list[MigrationReadinessAttestationItem] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationReadinessAttestation:
        d = _snake_keys(data)
        d["items"] = _model_list(MigrationReadinessAttestationItem, d.get("items", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class MigrationParityEvidenceArtifact:
    source_kind: str
    source: MigrationSourceIdentity
    overall_state: str
    summary: str
    cutover_readiness: MigrationCutoverReadinessSummary
    manifest_available: bool = False
    sections: list[MigrationParityEvidenceSection] = field(default_factory=list)
    artifact_kind: str = "honua.migration.parity-evidence-pack"
    artifact_version: str = "1.0"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MigrationParityEvidenceArtifact:
        d = _snake_keys(data)
        source = d.get("source")
        cutover_readiness = d.get("cutover_readiness")
        d["source"] = MigrationSourceIdentity.from_dict(source) if isinstance(source, dict) else source
        d["cutover_readiness"] = (
            MigrationCutoverReadinessSummary.from_dict(cutover_readiness)
            if isinstance(cutover_readiness, dict)
            else cutover_readiness
        )
        d.setdefault("manifest_available", False)
        d["sections"] = _model_list(MigrationParityEvidenceSection, d.get("sections", []))
        d.setdefault("artifact_kind", "honua.migration.parity-evidence-pack")
        d.setdefault("artifact_version", "1.0")
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_camel_dict(self)
        return {
            "artifactKind": d.pop("artifactKind"),
            "artifactVersion": d.pop("artifactVersion"),
            **d,
        }


#: ``artifactKind`` the server requires on an inbound toolbox translation manifest.
TOOLBOX_TRANSLATION_MANIFEST_KIND = "honua.migration.toolbox-translation"

#: ``artifactKind`` the server stamps on the validation report it returns.
TOOLBOX_TRANSLATION_REPORT_KIND = "honua.migration.toolbox-translation-report"

#: Manifest schema version this client speaks. The server rejects an
#: unrecognised version rather than reinterpreting the payload under v1.
TOOLBOX_TRANSLATION_ARTIFACT_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ToolboxParameterBinding:
    """Canonical parameter signature the server round-tripped for one mapping."""

    source_name: str
    target_parameter: str
    value_type: str
    required: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolboxParameterBinding:
        return cls(**_extract_fields(cls, _snake_keys(data)))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class ToolboxTranslationIssue:
    """One explicit unsupported-report entry the server attached to a tool.

    ``code`` is a stable machine-readable identifier (for example
    ``no-native-executor``, ``unknown-process``, ``missing-required-parameter``);
    treat unknown codes as opaque rather than switching on the message text.
    """

    code: str
    message: str
    parameter_name: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolboxTranslationIssue:
        return cls(**_extract_fields(cls, _snake_keys(data)))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class ToolboxToolTranslation:
    """Server verdict for a single toolbox tool.

    ``classification`` is one of ``translated`` / ``partially-translated`` /
    ``unsupported``.
    """

    tool_name: str
    classification: str
    process_id: str | None = None
    parameter_bindings: list[ToolboxParameterBinding] = field(default_factory=list)
    issues: list[ToolboxTranslationIssue] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolboxToolTranslation:
        d = _snake_keys(data)
        d["parameter_bindings"] = _model_list(ToolboxParameterBinding, d.get("parameter_bindings", []))
        d["issues"] = _model_list(ToolboxTranslationIssue, d.get("issues", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class ToolboxTranslationSummary:
    """Per-classification tool counts for a translation report."""

    tool_count: int = 0
    translated_count: int = 0
    partially_translated_count: int = 0
    unsupported_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolboxTranslationSummary:
        return cls(**_extract_fields(cls, _snake_keys(data)))

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass(frozen=True, slots=True)
class ToolboxTranslationReport:
    """Server-authoritative validation report for a translated toolbox.

    Returned by
    :meth:`~honua_admin.HonuaAdminClient.validate_toolbox_translation`. The
    canonical process catalog — not the SDK's local view of it — decides every
    ``classification`` here, which is what makes a report built from this
    *server-attested* rather than a local assertion.

    ``artifact_kind`` / ``artifact_version`` are deliberately **not** defaulted
    to the expected identity: they stay ``None`` when the response omitted them.
    The genuine endpoint always stamps both, so their absence is evidence that
    the payload is not a translation report — and stamping a plausible identity
    here would erase exactly the evidence a caller needs to refuse the response.
    :func:`honua_sdk.migration.attest_translation` requires both fields before it
    will call a verdict attested, and this round-trips their absence to it.
    """

    toolbox_name: str
    source_format: str
    summary: ToolboxTranslationSummary = field(default_factory=ToolboxTranslationSummary)
    tools: list[ToolboxToolTranslation] = field(default_factory=list)
    artifact_kind: str | None = None
    artifact_version: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ToolboxTranslationReport:
        d = _snake_keys(data)
        summary = d.get("summary")
        d["summary"] = (
            ToolboxTranslationSummary.from_dict(summary)
            if isinstance(summary, dict)
            else ToolboxTranslationSummary()
        )
        d["tools"] = _model_list(ToolboxToolTranslation, d.get("tools", []))
        return cls(**_extract_fields(cls, d))

    def to_dict(self) -> dict[str, Any]:
        # An omitted identity stays omitted, so a consumer of this dict sees the
        # response the server actually sent.
        d = _dataclass_to_camel_dict(self)
        identity: dict[str, Any] = {
            key: d.pop(key) for key in ("artifactKind", "artifactVersion") if key in d
        }
        return {**identity, **d}


# ---------------------------------------------------------------------------
# Request models (mutable)
# ---------------------------------------------------------------------------


@dataclass
class ToolboxParameterMapping:
    """Proposed mapping of one source toolbox parameter onto a process parameter."""

    source_name: str
    target_parameter: str
    source_data_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass
class ToolboxToolDescriptor:
    """One toolbox tool as translated by the SDK scanner.

    ``target_process_id`` is the canonical Honua process the scanner proposes as
    the native executor (for example ``geometry.buffer``). Leave it ``None`` when
    no native mapping was found — the server then reports the tool unsupported
    rather than stubbing it as executable.
    """

    tool_name: str
    display_name: str | None = None
    target_process_id: str | None = None
    parameter_mappings: list[ToolboxParameterMapping] = field(default_factory=list)
    unsupported_constructs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)


@dataclass
class ToolboxTranslationManifest:
    """SDK-emitted translation manifest for one ArcGIS toolbox.

    ``source_format`` is the source toolbox format: ``pyt``, ``atbx``, or
    ``tbx``. Redact local paths and credentials out of ``source_label`` before
    submitting — the server echoes it into operator-visible output.
    """

    toolbox_name: str
    source_format: str
    source_label: str | None = None
    tools: list[ToolboxToolDescriptor] = field(default_factory=list)
    artifact_kind: str = TOOLBOX_TRANSLATION_MANIFEST_KIND
    artifact_version: str = TOOLBOX_TRANSLATION_ARTIFACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        d = _dataclass_to_camel_dict(self)
        return {
            "artifactKind": d.pop("artifactKind"),
            "artifactVersion": d.pop("artifactVersion"),
            **d,
        }


@dataclass
class MigrationInventoryScanRequest:
    source_kind: str
    source_url: str
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    timeout_seconds: int | None = None
    include_style_content: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return _dataclass_to_camel_dict(self)
