"""Non-priced local Compose smoke against actual server and PostGIS images."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from live_observer import observe_database, observe_layer
from ga_actions import invoke
from live_probes import ProbeTraffic, assess_traffic, readback_writes
from real_seed import prepare_town


def main() -> None:
    root = Path(os.environ['E2E_OUT'])
    base, key = os.environ['E2E_BASE'], os.environ['E2E_API_KEY']
    evidence = {'project': os.environ['COMPOSE_PROJECT_NAME'], 'status': 'fail', 'layers': {}}
    try:
        seed = json.loads((root / 'seed-manifest.json').read_text())
        publication = json.loads((root / 'seed-inspections-publication.json').read_text())
        publication = publication.get('data', publication)
        namespace = 'ops_' + uuid.uuid4().hex[:12]
        town = prepare_town(variant='A', namespace=namespace, base_url=base, credential_env='E2E_API_KEY',
                            candidate_digest=os.environ['HONUA_SERVER_IMAGE'].split('@', 1)[1],
                            connection_id=seed['connectionId'], work=Path(os.environ['OPS_SMOKE_OUT']) / namespace,
                            project=os.environ['COMPOSE_PROJECT_NAME'], compose_file=Path(os.environ['E2E_COMPOSE_FILE']),
                            image_ref=os.environ['HONUA_SERVER_IMAGE'], authorize=lambda: None)
        evidence['town'] = town
        expected = {'parcels': 6, 'flood_zones': 1, 'shelters': 5}
        for name, binding in town['layers'].items():
            path = f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}"
            observed = observe_layer(base, path, key)
            evidence['layers'][name] = observed
            if observed['count'] != expected[name]:
                raise RuntimeError('live town feature count mismatch')
        path = f"/rest/services/maui-inspections/FeatureServer/{publication['layerId']}"
        traffic = ProbeTraffic(base, path, '', 'note',
                               {'x': -156.47, 'y': 20.89, 'spatialReference': {'wkid': 4326}},
                               write_attributes={'name': 'ops probe', 'category': 'park', 'status': 'ok',
                                                 'reported_at': int(time.time() * 1000)}, writer_interval=0.2)
        candidate = os.environ.get('OPS_SMOKE_CANDIDATE_IMAGE')
        action_args = None
        before_db = None
        backup = None
        if candidate:
            tables = sorted({(binding['schema'], binding['table']) for binding in town['layers'].values()} |
                            {('honua_data', 'e2e_src_fs'), ('honua_data', 'maui_zoning')})
            before_db = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['before_database'] = before_db
            action_out = Path(os.environ['OPS_SMOKE_OUT']) / 'actions'
            action_out.mkdir(mode=0o700)
            action_args = {'project': os.environ['COMPOSE_PROJECT_NAME'],
                           'compose_file': Path(os.environ['E2E_COMPOSE_FILE']), 'output': action_out,
                           'prior_image': os.environ['HONUA_SERVER_IMAGE'], 'candidate_image': candidate,
                           'base_url': base}
            backup = invoke(operation='backup', **action_args)
            evidence['backup'] = backup
            if not backup['ok']:
                raise RuntimeError('live pre-upgrade backup failed')
        traffic.start()
        try:
            time.sleep(1)
            if action_args:
                upgraded = invoke(operation='start-candidate', **action_args)
                evidence['upgrade'] = upgraded
                if not upgraded['ok']:
                    raise RuntimeError('live upgrade failed')
            time.sleep(1)
        finally:
            samples = traffic.stop()
            evidence['traffic'] = samples
        ids = [row['object_id'] for row in samples['samples']
               if row['kind'] in ('write', 'writer') and row['ok']]
        # The public write probe uses no credential; readback uses the controller key.
        readback = readback_writes(base, path, key, 'objectid', 'note', ids)
        evidence['readback'] = readback
        result = assess_traffic(samples, readback)
        evidence['assessment'] = result
        if not result['rpo_zero']:
            raise RuntimeError('live probe lost an acknowledged write')
        if action_args:
            after_upgrade = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['upgraded_database'] = after_upgrade
            def data_only(snapshot):
                return {name: (value['fields'], value['rows']) for name, value in snapshot.items()}
            if data_only(before_db) != data_only(after_upgrade):
                raise RuntimeError('seeded data changed on upgrade')
            restored = invoke(operation='restore-prior', expected_backup_sha256=backup['backup_sha256'], **action_args)
            evidence['restore'] = restored
            if not restored['ok']:
                raise RuntimeError('live backup restore failed')
            after_restore = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['restored_database'] = after_restore
            if data_only(before_db) != data_only(after_restore):
                raise RuntimeError('seeded data changed on restore')
        evidence['status'] = 'pass'
        print('real Compose smoke passed: town layers, anonymous public traffic, controller readback' +
              (', upgrade and backup restore' if action_args else ''))
    except Exception as exc:
        evidence['error_type'] = type(exc).__name__
        raise
    finally:
        (Path(os.environ['OPS_SMOKE_OUT']) / 'smoke-evidence.json').write_text(
            json.dumps(evidence, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
