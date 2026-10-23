"""Replay the native AnalysisPlan fingerprint from the original MCP request.

This is the byte contract written by GeoprocessingJobService, not the campaign
canonical-JSON digest. Only the named MCP enum values and its existing wire
fields are admitted. Raster bindings and protocol execution controls are not
part of that wire contract. A matching digest does not prove authorization,
execution, ownership, lifecycle, output CRS, or artifact content.
"""
from __future__ import annotations

import hashlib

from agent_e2e_pack import PackError

STEP_KINDS = ('QueryFeatures', 'Geoprocess', 'Aggregate', 'RenderMap', 'Export')
ARTIFACT_KINDS = ('Scalar', 'FeatureLayer', 'Table', 'Raster', 'File', 'Report', 'Map', 'AppBundle')


def require(condition, message):
    if not condition:
        raise PackError(message)


def text(value, *, nullable=False):
    if value is None and nullable:
        return ''
    require(isinstance(value, str), 'native plan fingerprint requires string fields')
    require(not any(0xD800 <= ord(c) <= 0xDFFF for c in value), 'unpaired UTF-16 input is not admitted')
    return value


def named_enum(value, names):
    value = text(value).strip()
    matches = [name for name in names if name.lower() == value.lower()]
    require(len(matches) == 1, 'native plan fingerprint requires a reviewed named enum')
    return matches[0]


def ordinal(value):
    # StringComparer.Ordinal sorts UTF-16 code units, not Unicode code points.
    return value.encode('utf-16-be')


def quote(value):
    """Utf8JsonWriter's default encoder, including HTML-sensitive ASCII."""
    short = {'\b': '\\b', '\t': '\\t', '\n': '\\n', '\f': '\\f', '\r': '\\r', '\\': '\\\\'}
    encoded = []
    for char in value:
        point = ord(char)
        if char in short:
            encoded.append(short[char])
        elif 32 <= point < 127 and char not in '\"&\'+<>`':
            encoded.append(char)
        else:
            units = char.encode('utf-16-be')
            encoded.extend('\\u'+units[i:i+2].hex().upper() for i in range(0, len(units), 2))
    return '\"'+''.join(encoded)+'\"'


def array(values):
    return '['+','.join(values)+']'


def object_fields(values):
    return '{'+','.join(quote(key)+':'+value for key, value in values)+'}'


def sequence(value):
    if value is None:
        return []
    require(isinstance(value, list), 'native plan collection must be a list or null')
    return value


def fingerprint_bytes(plan):
    require(isinstance(plan, dict) and set(plan) <= {'planId', 'intentId', 'steps', 'outputs', 'warnings'},
            'unsupported native MCP plan fields')
    steps = sequence(plan.get('steps'))
    require(len(steps) <= 256, 'native observer plan step bound exceeded')
    for warning in sequence(plan.get('warnings')):
        text(warning)
    encoded_steps = []
    for step in steps:
        require(isinstance(step, dict) and set(step) <= {'stepId', 'kind', 'processId', 'inputs', 'dependsOn'},
                'unsupported native MCP plan step fields')
        inputs = step.get('inputs')
        if inputs is None:
            inputs = {}
        require(isinstance(inputs, dict), 'native plan inputs must be a string dictionary')
        for key, value in inputs.items():
            text(key); text(value)
        dependencies = [text(value) for value in sequence(step.get('dependsOn'))]
        encoded_steps.append(object_fields([
            ('stepId', quote(text(step.get('stepId'), nullable=True))),
            ('kind', quote(named_enum(step.get('kind'), STEP_KINDS))),
            ('processId', quote(text(step.get('processId'), nullable=True))),
            ('inputs', array(object_fields([('Key', quote(key)), ('Value', quote(inputs[key]))])
                             for key in sorted(inputs, key=ordinal))),
            ('dependsOn', array(quote(value) for value in sorted(dependencies, key=ordinal))),
        ]))
    outputs = [named_enum(value, ARTIFACT_KINDS) for value in sequence(plan.get('outputs'))]
    result = object_fields([
        ('planId', quote(text(plan.get('planId'), nullable=True))),
        ('intentId', quote(text(plan.get('intentId'), nullable=True))),
        ('steps', array(encoded_steps)),
        ('outputs', array(quote(value) for value in sorted(outputs, key=ordinal))),
    ]).encode('ascii')
    require(len(result) <= 4*1024*1024, 'native observer fingerprint byte bound exceeded')
    return result


def fingerprint(plan):
    return hashlib.sha256(fingerprint_bytes(plan)).hexdigest()
