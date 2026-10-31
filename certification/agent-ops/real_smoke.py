"""Non-priced local Compose smoke against actual server and PostGIS images."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from live_observer import observe_layer
from live_probes import ProbeTraffic, assess_traffic, readback_writes
from real_seed import prepare_town


def main() -> None:
    root = Path(os.environ['E2E_OUT'])
    base, key = os.environ['E2E_BASE'], os.environ['E2E_API_KEY']
    seed = json.loads((root / 'seed-manifest.json').read_text())
    publication = json.loads((root / 'seed-inspections-publication.json').read_text())
    publication = publication.get('data', publication)
    namespace = 'ops_' + uuid.uuid4().hex[:12]
    town = prepare_town(variant='A', namespace=namespace, base_url=base, credential_env='E2E_API_KEY',
                        candidate_digest=os.environ['HONUA_SERVER_IMAGE'].split('@', 1)[1],
                        connection_id=seed['connectionId'], work=Path(os.environ['OPS_SMOKE_OUT']) / namespace,
                        project=os.environ['COMPOSE_PROJECT_NAME'], compose_file=Path(os.environ['E2E_COMPOSE_FILE']),
                        image_ref=os.environ['HONUA_SERVER_IMAGE'], authorize=lambda: None)
    expected = {'parcels': 6, 'flood_zones': 1, 'shelters': 5}
    for name, binding in town['layers'].items():
        path = f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}"
        observed = observe_layer(base, path, key)
        if observed['count'] != expected[name]:
            raise RuntimeError('live town feature count mismatch')
    path = f"/rest/services/maui-inspections/FeatureServer/{publication['layerId']}"
    traffic = ProbeTraffic(base, path, '', 'note',
                           {'x': -156.47, 'y': 20.89, 'spatialReference': {'wkid': 4326}},
                           write_attributes={'name': 'ops probe', 'category': 'park', 'status': 'ok',
                                             'reported_at': int(time.time() * 1000)}, writer_interval=0.2)
    traffic.start()
    time.sleep(2)
    samples = traffic.stop()
    ids = [row['object_id'] for row in samples['samples']
           if row['kind'] in ('write', 'writer') and row['ok']]
    # The public write probe uses no credential; readback uses the controller key.
    readback = readback_writes(base, path, key, 'objectid', 'note', ids)
    result = assess_traffic(samples, readback)
    if not result['rpo_zero']:
        raise RuntimeError('live probe lost an acknowledged write')
    print('real Compose smoke passed: town layers, customer traffic, controller readback')


if __name__ == '__main__':
    main()
