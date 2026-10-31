"""P05 CSV import remains unsupported.

The runner needs a role-bound SQL table identity; sealed reports and a read of
an exact customer attachment's imported table are retained independently.
"""
import hashlib
import re

from agent_e2e_local_api import Observation, Scenario, Seeded


class P05(Scenario):
    id = 'P05'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        return Seeded({'variant': variant})

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
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
                recorder.sql('p05-import-rows',
                    'SELECT source_id, name, capacity, is_open, ST_SRID(' + geometry +
                    ') AS srid, ST_AsGeoJSON(' + geometry + ')::json AS geometry FROM ' +
                    schema + '.' + table + ' ORDER BY source_id')
        return [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


scenario = P05()
