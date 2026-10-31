"""P04 vector import remains unsupported.

The runner needs SQL reads bound to agent-created table roles; typed GeoJSON
attributes required by the table checks depend on honua-server #6815.
"""
import hashlib
import re

from agent_e2e_local_api import Observation, Scenario, Seeded


class P04(Scenario):
    id = 'P04'
    supported_variants = ()

    def seed(self, stack, variant):
        if variant not in ('A', 'B'):
            raise ValueError('unsupported variant')
        return Seeded({'variant': variant})

    def observe(self, stack, seeded, variant, run=None):
        recorder = stack.extra['recorder']
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
            columns = 'source_id, name, owner, value' if label == 'parcels' else 'source_id, name'
            recorder.sql('p04-' + label + '-rows', 'SELECT ' + columns + ', ST_SRID(' + geometry +
                         ') AS srid, ST_AsGeoJSON(ST_Transform(' + geometry + ', 3857))::json AS geometry FROM ' +
                         schema + '.' + table + ' ORDER BY source_id')
        return [Observation('source-content.unchanged', None, {'projection': 'extract/v1'})]


scenario = P04()
