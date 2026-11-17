import assert from 'node:assert/strict';
import { test } from 'node:test';
import { publishedStorageId, fixtureTarget } from './layer-identity.mjs';

const row = (serviceName, serviceLayerIndex, serviceId = `svc-${serviceName}`) => ({ serviceId, serviceName, serviceLayerIndex });

test('the publish acknowledgement handle is storageLayerId, with the old layerId name as a fallback', () => {
  assert.equal(publishedStorageId({ storageLayerId: 7, layerName: 'parcels' }), 7);
  assert.equal(publishedStorageId({ layerId: 3 }), 3);
  assert.equal(publishedStorageId({ storageLayerId: 0 }), 0);
  assert.throws(() => publishedStorageId({ layerName: 'parcels' }), /no storage layer id/);
  assert.throws(() => publishedStorageId({ storageLayerId: '7' }), /no storage layer id/);
});

test('the MCP target is the single listed row of the service, addressed by serviceLayerIndex', () => {
  const target = fixtureTarget([row('other', 0), row('installer-qualified-fixture', 0)], 'installer-qualified-fixture', 12);
  assert.deepEqual(target, { serviceId: 'svc-installer-qualified-fixture', serviceLayerIndex: 0, storageLayerId: 12 });
});

test('zero or several rows for the service, or a row without an integer index, refuse', () => {
  assert.throws(() => fixtureTarget([row('other', 0)], 'installer-qualified-fixture', 1), /exactly one/);
  assert.throws(() => fixtureTarget([row('installer-qualified-fixture', 0), row('installer-qualified-fixture', 1)],
    'installer-qualified-fixture', 1), /exactly one/);
  assert.throws(() => fixtureTarget([{ serviceId: 's', serviceName: 'installer-qualified-fixture', layerId: 1 }],
    'installer-qualified-fixture', 1), /serviceLayerIndex/);
  assert.throws(() => fixtureTarget([row('installer-qualified-fixture', 0, '')], 'installer-qualified-fixture', 1), /serviceId/);
});
