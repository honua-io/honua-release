"""P05 unsupported: core needs attachment-digest action binding and structured geometry CRS evidence.

The exact input is joined to agent-created tables; typed attributes require honua-server #6815.
"""
import hashlib
import re
from pathlib import Path

from agent_e2e_local_api import Observation, Scenario, Seeded
from agent_e2e_local_expectations import load_roles

CORPUS = Path(__file__).resolve().parents[2] / 'certification/agent-e2e/corpus'
INVENTORY = "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = 'honua_data' ORDER BY table_name"


class P05(Scenario):
    id = 'P05'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        previous = stack.extra.get('p05Seed')
        if previous is not None:
            if previous['variant'] != variant:
                raise ValueError('case already seeded for another variant')
            return Seeded(previous)
        recorder = stack.extra['recorder']
        recorder.table_inventory('p05-table-baseline', 'honua_data')
        resources = self.expected(variant)['source-content.unchanged']['resources']
        state = {'variant': variant}
        stack.extra['p05Seed'] = state
        return Seeded(state)

    def roles(self, stack, seeded, variant, run=None):
        return load_roles(self.id)

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
        recorder.table_inventory('p05-table-after', 'honua_data')
        resources = self.expected(variant)['source-content.unchanged']['resources']
        actions = recorder.actions()
        for label, resource in zip(('valid', 'invalid'), resources):
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
            report = recorder.action_result('p05-' + label + '-report', matching[-1])
            message = report.json if report.status == 200 else None
            result = message.get('result', {}) if isinstance(message, dict) else {}
            content = result.get('structuredContent') if isinstance(result, dict) else None
            if (label != 'valid' or not isinstance(content, dict) or result.get('isError', False) is not False
                    or content.get('success') is not True):
                continue
            schema, table, geometry = (content.get(key) for key in ('schema', 'table', 'geometryColumn'))
            if all(isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,62}', v)
                   for v in (schema, table, geometry)):
                recorder.table_rows('p05-import-rows', schema, table, ('source_id', 'name', 'capacity', 'is_open', geometry))
        return [Observation(name, None, {'projection': 'extract/v1'})
                for name in ('features.exact', 'csv.invalid-reported', 'geometry.crs', 'source-content.unchanged')]

    def handoff(self, stack, seeded, variant, run=None):
        records = stack.extra['recorder'].records
        if not all(step in records for step in ('p05-import-rows', 'p05-invalid-report')):
            return None
        rows = {'body': 'p05-import-rows', 'pointer': ''}
        invalid_ids = {'customerFileColumn': f'town-{variant}/invalid-rows.csv',
                       'rows': {'body': 'p05-invalid-report', 'pointer': '/result/structuredContent/rowErrors/*/row'},
                       'column': 'source_id', 'firstDataRow': 1}
        return {'object': {'resources': {'list': []},
            'limitations': {'map': invalid_ids, 'to': {'object': {'code': {'item': ''}}}},
            'facts': {'object': {'counts': {'object': {'features': {'count': rows}, 'points': {'count': rows}}},
                                  'names': {'map': rows, 'to': {'item': '/name'}}}}}}


scenario = P05()
