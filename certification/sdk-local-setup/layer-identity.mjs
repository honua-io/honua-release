// Layer identity on the candidate server. The admin publish acknowledgement names the global
// storage handle storageLayerId (older servers: layerId). It addresses OGC tile collections and the
// MapLibre source-layer. The MCP map tools address a layer as serviceId + serviceLayerIndex.
export function publishedStorageId(ack) {
  const id = ack?.storageLayerId ?? ack?.layerId;
  if (!Number.isInteger(id)) throw new Error('publish acknowledgement carries no storage layer id');
  return id;
}

export function fixtureTarget(rows, serviceName, storageLayerId) {
  const matches = rows.filter(row => row?.serviceName === serviceName);
  if (matches.length !== 1) throw new Error(`expected exactly one listed layer for ${serviceName}, got ${matches.length}`);
  const [row] = matches;
  if (typeof row.serviceId !== 'string' || !row.serviceId) throw new Error('listed layer has no serviceId');
  if (!Number.isInteger(row.serviceLayerIndex)) throw new Error('listed layer has no integer serviceLayerIndex');
  return { serviceId: row.serviceId, serviceLayerIndex: row.serviceLayerIndex, storageLayerId };
}
