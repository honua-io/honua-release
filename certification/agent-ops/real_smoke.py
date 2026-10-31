"""Non-priced local Compose smoke against actual server and PostGIS images."""
from __future__ import annotations

import json
import base64
import os
import time
import uuid
from pathlib import Path

from live_observer import compare_source_geometry, observe_database, observe_layer
from real_controller import RealController
from live_probes import ProbeTraffic, assess_traffic, readback_writes
from real_seed import _verify_runtime, prepare_town
from real_credentials import mint_operator
from state import _projected_layer, messy_table


def main() -> None:
    root = Path(os.environ['E2E_OUT'])
    base, key = os.environ['E2E_BASE'], os.environ['E2E_API_KEY']
    evidence = {'project': os.environ['COMPOSE_PROJECT_NAME'], 'status': 'fail', 'layers': {}}
    controller = None
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
        def authorize():
            if _verify_runtime(os.environ['COMPOSE_PROJECT_NAME'], Path(os.environ['E2E_COMPOSE_FILE']),
                               os.environ['HONUA_SERVER_IMAGE'], os.environ['HONUA_SERVER_IMAGE'].split('@', 1)[1], base) != town['runtime_identity']:
                raise RuntimeError('trial runtime changed')
        def retain_credential(kind, body):
            target = Path(os.environ['OPS_SMOKE_OUT']) / ('credential-' + kind + '.json')
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(body, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            encoded = json.dumps(body, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()
            import hashlib
            return {'key': target.name, 'sha256': 'sha256:' + hashlib.sha256(encoded).hexdigest()}
        operator = mint_operator(base, key, project=os.environ['COMPOSE_PROJECT_NAME'],
                                 compose_file=Path(os.environ['E2E_COMPOSE_FILE']),
                                 image_ref=os.environ['HONUA_SERVER_IMAGE'], authorize=authorize, retain=retain_credential)
        evidence['operator'] = operator.public_receipt()
        expected = {'parcels': 6, 'flood_zones': 1, 'shelters': 5}
        for name, binding in town['layers'].items():
            path = f"/rest/services/{binding['service']}/FeatureServer/{binding['layerId']}"
            observed = observe_layer(base, path, key)
            evidence['layers'][name] = observed
            if observed['count'] != expected[name]:
                raise RuntimeError('live town feature count mismatch')
            source_path = Path(os.environ['OPS_SMOKE_OUT']) / namespace / 'town-A' / f'{name}.4326.geojson'
            source = json.loads(source_path.read_text())['features']
            evidence['layers'][name]['source_geometry'] = compare_source_geometry(
                os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], binding['table'], source, srid=4326)
            expected_values = {feature['properties']['source_id']: feature['properties'] for feature in source}
            actual_values = {feature['attributes'].get('source_id'): feature['attributes'] for feature in observed['features']}
            if set(actual_values) != set(expected_values) or any(
                any(actual_values[identity].get(field) != value for field, value in values.items())
                for identity, values in expected_values.items()
            ):
                raise RuntimeError('live town attribute values differ from source')
            types = {field.get('name'): field.get('type') for field in observed['fields']}
            expected_types = {'source_id': 'esriFieldTypeString', 'name': 'esriFieldTypeString'}
            if name == 'parcels':
                expected_types.update(owner='esriFieldTypeString', value='esriFieldTypeInteger')
            if name == 'shelters':
                expected_types.update(capacity='esriFieldTypeInteger', is_open='esriFieldTypeSmallInteger')
            if any(types.get(field) != kind for field, kind in expected_types.items()):
                raise RuntimeError('live town field types differ from source fixture')
        station = town['stations']
        station_path = f"/rest/services/{station['service']}/FeatureServer/{station['layerId']}"
        evidence['layers']['stations'] = observe_layer(base, station_path, key)
        station_observation = evidence['layers']['stations']
        metadata = json.loads(base64.b64decode(station_observation['evidence'][0]['body_b64']))
        if metadata.get('spatialReference', {}).get('wkid') != 3338:
            raise RuntimeError('projected station CRS mismatch')
        station_types = {field.get('name'): field.get('type') for field in station_observation['fields']}
        if any(station_types.get(name) != kind for name, kind in {
            'source_id': 'esriFieldTypeString', 'name': 'esriFieldTypeString',
            'latitude': 'esriFieldTypeDouble', 'elevation_m': 'esriFieldTypeInteger',
        }.items()):
            raise RuntimeError('projected station field types mismatch')
        expected_stations = {f['properties']['source_id']: f for f in _projected_layer('Kestrel')['features']}
        actual_stations = {}
        for feature in station_observation['features']:
            attributes = feature['attributes']
            source_id = attributes.get('source_id')
            if source_id in actual_stations:
                raise RuntimeError('duplicate projected station')
            actual_stations[source_id] = (attributes, feature['geometry'])
        if set(actual_stations) != set(expected_stations):
            raise RuntimeError('projected station identity mismatch')
        for source_id, source in expected_stations.items():
            attributes, geometry = actual_stations[source_id]
            x, y = source['geometry']['coordinates']
            if any(attributes.get(k) != v for k, v in source['properties'].items()) or geometry.get('x') != x or geometry.get('y') != y:
                raise RuntimeError('projected station values or geometry mismatch')
        mixed = town['mixed_schema']
        mixed_snapshot = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'],
                                          [(mixed['schema'], mixed['table'])])
        evidence['mixed_schema'] = mixed_snapshot
        mixed_value = mixed_snapshot[f"{mixed['schema']}.{mixed['table']}"]
        expected_types = {'permit_id': 'text', 'year': 'integer', 'fee': 'numeric',
                          'note': 'text', 'active': 'boolean', 'owner': 'text'}
        if ({field['name']: field['type'] for field in mixed_value['fields']} != expected_types or
            sorted(mixed_value['rows'], key=lambda row: row['permit_id']) !=
                sorted(messy_table('Kestrel'), key=lambda row: row['permit_id'])):
            raise RuntimeError('mixed-schema fields or values mismatch')
        path = f"/rest/services/maui-inspections/FeatureServer/{publication['layerId']}"
        traffic = ProbeTraffic(base, path, '', 'note',
                               {'x': -156.47, 'y': 20.89, 'spatialReference': {'wkid': 4326}},
                               write_attributes={'name': 'ops probe', 'category': 'park', 'status': 'ok',
                                                 'reported_at': int(time.time() * 1000)}, writer_interval=0.2)
        candidate = os.environ.get('OPS_SMOKE_CANDIDATE_IMAGE')
        before_db = None
        backup = None
        if candidate:
            tables = sorted({(binding['schema'], binding['table']) for binding in town['layers'].values()} |
                            {(station['schema'], station['table']), (mixed['schema'], mixed['table']),
                             ('honua_data', 'e2e_src_fs'), ('honua_data', 'maui_zoning')})
            before_db = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['before_database'] = before_db
            action_out = Path(os.environ['OPS_SMOKE_OUT']) / 'actions'
            action_out.mkdir(mode=0o700)
            controller = RealController(project=os.environ['COMPOSE_PROJECT_NAME'],
                                        compose_file=Path(os.environ['E2E_COMPOSE_FILE']), output=action_out,
                                        prior_image=os.environ['HONUA_SERVER_IMAGE'], candidate_image=candidate,
                                        base_url=base)
            # Exercise the same structured action interface a model would use.
            # A rejected action must also appear in the controller's audit.
            rejected = controller.apply('honua_deploy_operations', {'operation': 'apply', 'image': candidate})
            if rejected.get('ok') is not False or rejected.get('error') != 'pre-upgrade-backup-required':
                raise RuntimeError('unsafe pre-backup update was not rejected')
            backup = controller.apply('honua_deploy_operations', {'operation': 'backup'})
            evidence['backup'] = backup
            if not backup['ok']:
                raise RuntimeError('live pre-upgrade backup failed')
        traffic.start()
        try:
            time.sleep(1)
            if controller:
                upgraded = controller.apply('honua_deploy_operations', {'operation': 'apply', 'image': candidate})
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
        if controller:
            after_upgrade = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['upgraded_database'] = after_upgrade
            def data_only(snapshot):
                return {name: (value['fields'], value['rows']) for name, value in snapshot.items()}
            if data_only(before_db) != data_only(after_upgrade):
                raise RuntimeError('seeded data changed on upgrade')
            restored = controller.apply('honua_deploy_operations', {'operation': 'restore_backup', 'backup_id': 'previous-release'})
            evidence['restore'] = restored
            if not restored['ok']:
                raise RuntimeError('live backup restore failed')
            after_restore = observe_database(os.environ['COMPOSE_PROJECT_NAME'], os.environ['E2E_COMPOSE_FILE'], tables)
            evidence['restored_database'] = after_restore
            if data_only(before_db) != data_only(after_restore):
                raise RuntimeError('seeded data changed on restore')
            evidence['action_audit'] = controller.audit
            if [event['ok'] for event in controller.audit] != [False, True, True, True]:
                raise RuntimeError('controller action audit is incomplete')
        evidence['status'] = 'operations-pass'
        evidence['cleanup'] = 'pending'
        print('real Compose operations completed: town layers, anonymous public traffic, controller readback' +
              (', upgrade and backup restore' if controller else ''))
    except Exception as exc:
        evidence['error_type'] = type(exc).__name__
        raise
    finally:
        if controller is not None:
            evidence['action_audit'] = controller.audit
        (Path(os.environ['OPS_SMOKE_OUT']) / 'smoke-evidence.json').write_text(
            json.dumps(evidence, sort_keys=True) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
