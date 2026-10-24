"""Actual PostGIS output readback under the separate observer identity."""
import copy
from uuid import uuid4

import pytest

from agent_e2e_import_observer import SqlImportedDataset
from agent_e2e_pack import PackError
from test_agent_e2e_fixture_database import postgres, fixture_db


@pytest.fixture
def imported_sql(fixture_db):
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Jsonb
    database, dsn = fixture_db
    with psycopg.connect(dsn) as connection:
        connection.execute('CREATE EXTENSION IF NOT EXISTS postgis')
    baseline = database.baseline()
    name = 'actual_import_'+uuid4().hex[:12]
    properties = {'source_id': 'native-point-1', 'capacity': None, 'title': 'Preserve every original field'}
    with psycopg.connect(dsn) as connection:
        connection.execute(sql.SQL('CREATE TABLE {} (id SERIAL PRIMARY KEY, geometry geometry, properties jsonb, created_at timestamptz DEFAULT now())').format(sql.Identifier('honua_data', name)))
        connection.execute(sql.SQL('COMMENT ON TABLE {} IS {}').format(sql.Identifier('honua_data', name), sql.Literal('honua:import-owned:v1')))
        connection.execute(sql.SQL('INSERT INTO {}(geometry,properties) VALUES (ST_SetSRID(ST_MakePoint(100.25,200.5),3857),%s)').format(sql.Identifier('honua_data', name)), (Jsonb(properties),))
        connection.execute(sql.SQL('GRANT SELECT ON {} TO fixture_observer').format(sql.Identifier('honua_data', name)))
    receipt = {'physicalTableName': name, 'schema': 'honua_data', 'featureCount': 1, 'detectedSrid': 4326}
    return database, dsn, baseline, receipt, properties


def test_actual_native_physical_output_preserves_properties_geometry_srid_and_observer_identity(imported_sql):
    database, _, baseline, receipt, properties = imported_sql
    observed = SqlImportedDataset(database, baseline=baseline).observe(receipt)
    assert observed['observerPrincipal'] == 'fixture_observer'
    assert observed['features'] == {'srid': 3857, 'features': [{'type': 'Feature', 'id': properties['source_id'],
        'properties': properties, 'geometry': {'type': 'Point', 'coordinates': [100.25, 200.5]}}]}
    assert observed['table']['marker'] == 'honua:import-owned:v1' and observed['table']['oid'] > 0


@pytest.mark.parametrize('change', ['preexisting', 'count', 'marker', 'row-security'])
def test_actual_sql_refuses_shared_mislabeled_filtered_or_incomplete_output(imported_sql, change):
    import psycopg
    from psycopg import sql
    database, dsn, baseline, receipt, _ = imported_sql
    if change == 'preexisting': baseline = database.baseline()
    elif change == 'count': receipt = {**receipt, 'featureCount': 2}
    else:
        with psycopg.connect(dsn) as connection:
            if change == 'marker':
                connection.execute(sql.SQL('COMMENT ON TABLE {} IS NULL').format(sql.Identifier('honua_data', receipt['physicalTableName'])))
            else:
                connection.execute(sql.SQL('ALTER TABLE {} ENABLE ROW LEVEL SECURITY').format(sql.Identifier('honua_data', receipt['physicalTableName'])))
    with pytest.raises(PackError): SqlImportedDataset(database, baseline=baseline).observe(receipt)
