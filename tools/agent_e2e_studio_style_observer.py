"""Project actual saved MapLibre style after an observed model mutation.

Expected colors, opacity, labels, document hashes and pixels remain source-frozen.
A style reference or model claim alone never supplies the rendered document.
"""
from __future__ import annotations

import copy
import math
import re

from agent_e2e_pack import PackError, canonical_digest
from agent_e2e_studio_composition_observer import MUTATIONS, SAVE
from agent_e2e_studio_observer import guid


def require(value, reason):
    if not value:
        raise PackError(reason)


def validate(config):
    require(isinstance(config, dict) and set(config) == {'floodLayerId', 'shelterLayerId'} and
        all(isinstance(value, str) and 0 < len(value) <= 200 and
            all(ord(c) >= 32 and ord(c) != 127 for c in value) for value in config.values()) and
        config['floodLayerId'] != config['shelterLayerId'], 'style observation requires two exact source-frozen composition layers')


def document(resource, config):
    require(isinstance(resource, dict) and isinstance(resource.get('envelope'), dict),
            'native style resource lacks its package envelope')
    envelope = resource['envelope']
    body = envelope.get('body')
    require(isinstance(body, dict), 'native style body is not an observed document')
    layers, style = body.get('layers'), body.get('mapSpec')
    require(isinstance(layers, list) and 1 <= len(layers) <= 100 and
        all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in layers) and
        len({row['id'] for row in layers}) == len(layers) and
        {config['floodLayerId'], config['shelterLayerId']} <= {row['id'] for row in layers},
        'native style lacks its exact composed flood/shelter layers')
    require(isinstance(style, dict) and type(style.get('version')) is int and style['version'] == 8 and
        isinstance(style.get('sources'), dict) and style['sources'] and
        isinstance(style.get('layers'), list) and 1 <= len(style['layers']) <= 256 and
        all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id'] for row in style['layers']) and
        len({row['id'] for row in style['layers']}) == len(style['layers']),
        'saved native map lacks its actual bounded MapLibre document')
    return style


def preflight(*, config, initial, expected, item_id):
    validate(config)
    style = document(initial, config)
    require(isinstance(expected, dict) and {'mapId', 'styleDocumentSha256', 'labelField', 'floodColor', 'floodOpacity'} <= set(expected) and
        expected['mapId'] == item_id == initial.get('itemId') and
        isinstance(expected['styleDocumentSha256'], str) and re.fullmatch('[a-f0-9]{64}', expected['styleDocumentSha256']),
        'style task lacks independent source-frozen expected content and exact map identity')
    require(canonical_digest(style)[7:] != expected['styleDocumentSha256'],
            'prepared fixture already contains the completed style task')
    initial = project(style, config, require_label=False)
    require(any(canonical_digest(initial[key]) != canonical_digest(expected[key]) for key in
                ('labelField', 'floodColor', 'floodOpacity')),
            'prepared fixture already contains the completed style task semantics')


def label_field(expression):
    # These are actual field lookups, not a literal string that happens to say
    # "name" or an unexecuted expression buried in a conditional branch.
    if isinstance(expression, list) and len(expression) == 2 and expression[0] == 'to-string':
        expression = expression[1]
    if isinstance(expression, list) and len(expression) == 2 and expression[0] == 'get':
        field = expression[1]
    elif isinstance(expression, str):
        match = re.fullmatch(r'[^{}]*\{([^{}]+)\}[^{}]*', expression)
        field = match[1] if match else None
    else:
        field = None
    require(isinstance(field, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,62}', field),
            'label is not an observed supported source-field expression')
    return field


def project(style, config, *, require_label=True):
    def owner(row):
        metadata = row.get('metadata', {})
        require(isinstance(metadata, dict), 'map style ownership metadata is malformed')
        declared = metadata.get('honua:compositionLayerId')
        if row['id'] in config.values():
            require(declared in (None, row['id']), 'map render layer contradicts its composition ownership')
            return row['id']
        return declared
    floods = [row for row in style['layers'] if owner(row) == config['floodLayerId'] and row.get('type') == 'fill']
    shelters = [row for row in style['layers'] if owner(row) == config['shelterLayerId']]
    require(len(floods) == 1 and shelters, 'map style has ambiguous or missing actual flood/shelter render layers')
    require(all(isinstance(row.get('source'), str) and row['source'] in style['sources'] for row in [*floods, *shelters]) and
        len({row['source'] for row in shelters}) == 1, 'shelter label is attached to another or missing data source')
    flood = floods[0]
    paint, layout = flood.get('paint', {}), flood.get('layout', {})
    require(isinstance(paint, dict) and isinstance(layout, dict) and layout.get('visibility', 'visible') == 'visible' and
        isinstance(paint.get('fill-color'), str) and 0 < len(paint['fill-color']) <= 128 and
        type(paint.get('fill-opacity')) in (int, float) and math.isfinite(paint['fill-opacity']) and
        0 <= paint['fill-opacity'] <= 1, 'flood style lacks explicit actual color/opacity')
    labels = [row for row in shelters if row.get('type') == 'symbol' and
              isinstance(row.get('layout'), dict) and 'text-field' in row['layout']]
    require(len(labels) == 1 or not require_label and not labels, 'shelter names need one actual owned symbol label layer')
    field = None
    if labels:
        label = labels[0]
        label_paint = label.get('paint', {})
        require(label['layout'].get('visibility', 'visible') == 'visible' and isinstance(label_paint, dict) and
            type(label_paint.get('text-opacity', 1)) in (int, float) and
            math.isfinite(label_paint.get('text-opacity', 1)) and 0 < label_paint.get('text-opacity', 1) <= 1,
            'saved shelter label is hidden or transparent')
        field = label_field(label['layout']['text-field'])
    return {'styleDocumentSha256': canonical_digest(style)[7:],
        'labelField': field,
        'floodColor': paint['fill-color'], 'floodOpacity': paint['fill-opacity']}


def observe(observer, *, config, initial, saved, save_selection, selections):
    validate(config)
    observer.owned(initial)
    observer.owned(saved)
    require(guid(initial.get('versionId')) != guid(saved.get('versionId')) and
        type(initial.get('versionNumber')) is int and type(saved.get('versionNumber')) is int and
        saved['versionNumber'] > initial['versionNumber'], 'style task must save a demonstrably new version')
    initial_style, style = document(initial, config), document(saved, config)
    require(canonical_digest(initial_style) != canonical_digest(style), 'unchanged prepared style cannot satisfy model styling')
    before, after = project(initial_style, config, require_label=False), project(style, config)
    require(any(canonical_digest(before[key]) != canonical_digest(after[key]) for key in
                ('labelField', 'floodColor', 'floodOpacity')), 'style task semantics were already complete before the model mutation')
    require(save_selection.get('tool') == SAVE and canonical_digest(save_selection.get('resource')) == canonical_digest(saved) and
        type(save_selection.get('ordinal')) is int and save_selection['ordinal'] > 0,
        'style observation lacks the original model save selection')
    mutation = [row for row in selections if row.get('tool') in MUTATIONS and
        type(row.get('ordinal')) is int and row['ordinal'] < save_selection['ordinal'] and
        isinstance(row.get('resource'), dict) and row['resource'].get('draftId') == saved.get('sourceDraftId')]
    require(mutation, 'saved style was not authored by an observed model mutation on this draft')
    selected = max(mutation, key=lambda row: row['ordinal'])
    resource = observer.owned(selected['resource'])
    arguments = save_selection.get('request', {}).get('input', {}).get('arguments', {})
    require(type(resource.get('generation')) is int and type(arguments.get('generation')) is int and
        resource['generation'] == arguments['generation'] and arguments.get('draftId') == saved.get('sourceDraftId') and
        resource.get('updatedBy') == observer.binding['ownerPrincipalId'] and
        canonical_digest(document(resource, config)) == canonical_digest(style) and
        isinstance(selected.get('nativeReferences', {}).get('draft'), dict),
        'saved style differs from its original native actor/draft/generation mutation')
    actual = {'mapId': observer.binding['itemId'], **project(style, config)}
    return {'actual': actual, 'mutationActionId': selected['actionId'], 'saveActionId': save_selection['actionId'],
        'versionId': saved['versionId'], 'sourceDraftId': saved['sourceDraftId'],
        'mutationReference': copy.deepcopy(selected['nativeReferences']['draft'])}
