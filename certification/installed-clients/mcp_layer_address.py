"""Publication address for the MCP map tools.

Admin publish returns storageLayerId. honua_query_features and honua_render_map address the
publication as serviceId + serviceLayerIndex, the pair honua_list_layers returns and
sdk-local-setup/layer-identity.mjs fixtureTarget selects by service name.
"""
from __future__ import annotations

from typing import Any, Callable

PAGE_LIMIT = 100
MAX_PAGES = 20


def publication_target(rows: list[Any], service_name: str) -> dict[str, Any]:
    """The one listed layer for a service. A storage id on the row is not the index."""
    matches = [row for row in rows if isinstance(row, dict) and row.get("serviceName") == service_name]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one listed layer for {service_name}, got {len(matches)}")
    row = matches[0]
    service_id = row.get("serviceId")
    index = row.get("serviceLayerIndex")
    if not isinstance(service_id, str) or not service_id or type(index) is not int or index < 0:
        raise ValueError("listed layer has no integer serviceLayerIndex")
    return {"serviceId": service_id, "serviceLayerIndex": index}


def address_for_service(call: Callable[[dict[str, Any]], dict[str, Any]], service_name: str) -> dict[str, Any]:
    """Page honua_list_layers and return the publication address for service_name."""
    if not isinstance(service_name, str) or not service_name:
        raise ValueError("service name is required")
    rows: list[Any] = []
    offset = 0
    for page in range(MAX_PAGES):
        listing = call({"filter": service_name, "limit": PAGE_LIMIT, "offset": offset})
        layers = listing.get("layers") if isinstance(listing, dict) else None
        if not isinstance(layers, list):
            raise ValueError("layer list has no layers")
        rows.extend(layers)
        if listing.get("hasMore") is False:
            return publication_target(rows, service_name)
        next_offset = listing.get("nextOffset")
        if type(next_offset) is not int or next_offset <= offset or page == MAX_PAGES - 1:
            raise ValueError("layer list did not finish")
        offset = next_offset
    raise ValueError("layer list did not finish")


def _require(target: dict[str, Any]) -> tuple[str, int]:
    if not isinstance(target, dict) or set(target) != {"serviceId", "serviceLayerIndex"}:
        raise ValueError("map tools take serviceId and serviceLayerIndex")
    service_id, index = target["serviceId"], target["serviceLayerIndex"]
    if not isinstance(service_id, str) or not service_id or type(index) is not int or index < 0:
        raise ValueError("listed layer has no integer serviceLayerIndex")
    return service_id, index


def query_arguments(target: dict[str, Any], *, where: str, limit: int) -> dict[str, Any]:
    service_id, index = _require(target)
    return {"serviceId": service_id, "serviceLayerIndex": index, "where": where, "limit": limit}


def render_layer(target: dict[str, Any]) -> dict[str, Any]:
    service_id, index = _require(target)
    return {"serviceId": service_id, "serviceLayerIndex": index}


def rendered_image(result: dict[str, Any]) -> dict[str, Any]:
    """Extract the inlined PNG payload and media type from an MCP honua_render_map result."""
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    image = structured.get("image") if isinstance(structured, dict) else None
    image = image if isinstance(image, dict) else {}
    declared_format = image.get("format") if isinstance(image.get("format"), str) else None
    structured_base64 = image.get("base64") if isinstance(image.get("base64"), str) else None
    content = result.get("content") if isinstance(result, dict) else None
    blocks = [item for item in (content if isinstance(content, list) else [])
              if isinstance(item, dict) and item.get("type") == "image"]
    if len(blocks) > 1:
        return {"png": None, "mimeType": declared_format}
    if len(blocks) == 1:
        block = blocks[0]
        block_mime = block.get("mimeType") if isinstance(block.get("mimeType"), str) else None
        if block_mime and declared_format and block_mime != declared_format:
            return {"png": None, "mimeType": None}
        block_data = block.get("data") if isinstance(block.get("data"), str) else None
        if structured_base64 is not None and structured_base64 != block_data:
            return {"png": None, "mimeType": block_mime or declared_format}
        return {"png": block_data, "mimeType": block_mime or declared_format}
    return {"png": structured_base64, "mimeType": declared_format}
