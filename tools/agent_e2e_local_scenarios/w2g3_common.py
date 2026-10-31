"""TownSetup limited to the requested A/B fixture files."""
from pathlib import Path

from agent_e2e_pack import load_json, verify_corpus
from agent_e2e_scenario_backend import TownSetup

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / 'certification/agent-e2e/corpus/manifest.v1.json'


def town_setup(recorder, variant):
    if variant not in ('A', 'B'):
        raise ValueError('only A/B fixtures are permitted')
    manifest = load_json(MANIFEST)
    names = {f'town-{variant}/{name}.3857.geojson' for name in ('parcels', 'flood_zones', 'shelters')}
    rows = [row for row in manifest['files'] if row['path'] in names]
    if len(rows) != len(names) or {row['path'] for row in rows} != names:
        raise ValueError('selected town manifest is incomplete or duplicated')
    verify_corpus(MANIFEST.parent, {'files': rows})
    town = TownSetup.__new__(TownSetup)
    town.http = recorder
    town.manifest_path = MANIFEST
    town.manifest = {'files': rows}
    town.files = {row['path']: row for row in rows}
    return town
