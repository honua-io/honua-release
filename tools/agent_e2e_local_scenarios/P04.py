"""P04 unsupported: core needs attachment-digest action binding and structured geometry CRS evidence.

The runner needs SQL reads bound to agent-created table roles; typed GeoJSON
attributes required by the table checks depend on honua-server #6815.
"""
import hashlib
import json
import re
from pathlib import Path

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles

CORPUS = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus'
INVENTORY = "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = 'honua_data' ORDER BY table_name"


class P04(Scenario):
    id = 'P04'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        previous = stack.extra.get('p04Seed')
        if previous is not None:
            if previous['variant'] != variant:
                raise ValueError('case already seeded for another variant')
            return Seeded(previous)
        recorder = stack.extra['recorder']
        recorder.table_inventory('p04-table-baseline', 'honua_data')
        state = {'variant': variant}
        stack.extra['p04Seed'] = state
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        recorder.table_inventory('p04-table-after', 'honua_data')
        resources = self.expected(variant)['source-content.unchanged']['resources']
        actions = recorder.actions()
        for label, resource in zip(('parcels', 'flood-zones'), resources):
            expected_digest = resource['content']['sha256']
            matching = []
            for action in actions:
                args = action.arguments if isinstance(action.arguments, dict) else {}
                data = args.get('data')
                if (action.kind == 'tool_call' and action.tool == 'honua_ingest_dataset'
                        and isinstance(data, str)
                        and 'sha256:' + hashlib.sha256(data.encode('utf-8')).hexdigest() == expected_digest):
                    matching.append(action)
            if not matching:
                continue
            report = recorder.action_result('p04-' + label + '-report', matching[-1])
            message = report.json if report.status == 200 else None
            result = message.get('result', {}) if isinstance(message, dict) else {}
            content = result.get('structuredContent') if isinstance(result, dict) else None
            if not isinstance(content, dict) or result.get('isError', False) is not False or content.get('success') is not True:
                continue
            schema, table, geometry = (content.get(key) for key in ('schema', 'table', 'geometryColumn'))
            if not all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v)
                       for v in (schema, table, geometry)):
                continue
            columns = ('source_id', 'name', 'owner', 'value', geometry) if label == 'parcels' else ('source_id', 'name', geometry)
            recorder.table_rows('p04-' + label + '-rows', schema, table, columns)
        return [Observation(name, None, {'projection': 'extract/v1'})
                for name in ('features.exact', 'geometry.crs', 'source-content.unchanged')]

    def handoff(self, stack, seeded, variant, run=None):
        records = stack.extra['recorder'].records
        if not all('p04-' + label + '-rows' in records for label in ('parcels', 'flood-zones')):
            return None
        rows = {'concat': [{'body': 'p04-parcels-rows', 'pointer': ''},
                           {'body': 'p04-flood-zones-rows', 'pointer': ''}]}
        return {'object': {'resources': {'list': []}, 'facts': {'object': {
            'counts': {'object': {'features': {'count': rows}}},
            'names': {'map': rows, 'to': {'item': '/name'}}}}}}


scenario = P04()
