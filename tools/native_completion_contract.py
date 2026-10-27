#!/usr/bin/env python3
"""Build a pinned original native scope and a non-certifying completion backlog."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

import desktop_qualification as desktop
from check_protocol_certification import _requirement_signature, _owned_source_name, REQUIREMENTS_SCHEMA_ID


def need(ok, message):
    if not ok:
        raise ValueError(message)


def captured(path, pin):
    value, digest = desktop.read_input(Path(path))
    need(re.fullmatch(r'[0-9a-f]{64}', pin or '') and digest == pin, 'Input SHA256 differs')
    return value


def requirement_id(row):
    payload = json.dumps(_requirement_signature(row), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def py_projection(registry, profile, expected):
    need(registry.get('schema') == 'honua.native-operation-crosswalk/v1'
         and profile.get('schema') == 'honua.native-certification-profile/v1'
         and profile.get('driver') == 'pyqgis' and registry.get('profile_revision') == profile.get('revision'),
         'Py registry/profile identity differs')
    cells = profile['required_cells']
    need(all(isinstance(c, list) and len(c) == 3 and c[2] == 'pyqgis' for c in cells), 'Invalid Py cell')
    source_cells = {'/'.join(c) for c in cells}
    need(len(source_cells) == len(cells) and {'pyqgis/'+ '/'.join(c[:2]) for c in cells} == expected,
         'Py profile differs from original required cells')
    processes = registry['processes']; mapping = registry['cell_requirements']
    need(isinstance(processes, dict) and isinstance(mapping, dict) and set(mapping) <= source_cells,
         'Invalid Py crosswalk')
    for key, process in processes.items():
        assertions = process['required_case_ids']
        need(process['id'] == key and isinstance(assertions, list) and assertions
             and len(assertions) == len(set(assertions)) and all(isinstance(x, str) and x for x in assertions)
             and set('/'.join(c) for c in process['cells']) <= source_cells, 'Invalid Py process')
    result = {}
    for row, column, _ in cells:
        cell_id = f'pyqgis/{row}/{column}'; native = f'{row}/{column}/pyqgis'
        children = mapping.get(native, [])
        need(isinstance(children, list) and len(children) == len(set(children)) and set(children) <= set(processes),
             'Invalid Py cell process binding')
        result[cell_id] = [{'case_id':'pyqgis/'+case, 'assertion_id':assertion}
                           for case in children for assertion in processes[case]['required_case_ids']]
    declared = {'/'.join(c) for c in registry['unmapped_cells']}
    need(declared == {name for name in source_cells if not mapping.get(name)}, 'Py unmapped inventory differs')
    return result


def arc_projection(projection, expected):
    need(projection.get('schema') == 'honua.native-accounting-inventory/v1'
         and projection.get('lane') == 'arcpy', 'Authoritative Arc projection required')
    cases = {item['case_id'] for item in projection['cases']}
    cells = projection['cells']
    need(len(cells) == len(expected) and {c['cell_id'] for c in cells} == expected,
         'Arc projection differs from original required cells')
    result = {}
    for cell in cells:
        children = cell['required_children']
        need(cell['mapping_status'] in ('mapped', 'unmapped') and isinstance(children, list)
             and (cell['mapping_status'] != 'mapped' or children), 'Invalid Arc mapping')
        pairs = [(c['case_id'], c['assertion_id']) for c in children]
        need(len(pairs) == len(set(pairs)) and all(c in cases and isinstance(a, str) and a for c, a in pairs),
             'Arc child is not in projected case inventory')
        result[cell['cell_id']] = [{'case_id':'arcpy/'+c, 'assertion_id':a} for c,a in pairs]
    return result


def build(scope, grids, registry, profile, requirements, candidate_digest, integration_source,
          arc=None, bindings=None, input_sha256=None):
    need(re.fullmatch(r'[0-9a-f]{40}', integration_source or ''), 'Full integration source commit required')
    progress = desktop.report(scope, grids, candidate_digest)
    need(progress['required_count'] == 950 and sum(v['deferred'] for v in progress['summaries'].values()) == 14,
         'Original 950 required plus 14 deferred scope required')
    cells = [{'cell_id': ('arcpy' if c['source']=='licensed-scripting' else 'pyqgis')+'/'+c['row']+'/'+c['column'],
              'driver': ('arcpy' if c['source']=='licensed-scripting' else 'pyqgis'),
              'row':c['row'], 'column':c['column']} for c in progress['cells'] if c['required']]
    ids = {c['cell_id'] for c in cells}; counts = Counter(c['driver'] for c in cells)
    need(len(ids) == 950 and counts == {'arcpy':477, 'pyqgis':473}, 'Original cell identities differ')
    native_scope = {'schema':'honua.full-development-jit-certification-scope/v1',
        'integration_source':integration_source, 'scope_counts':dict(counts),
        'total_applicable_non_sensor_cells':950, 'cells':cells}
    mappings = py_projection(registry, profile, {i for i in ids if i.startswith('pyqgis/')})
    if arc is not None: mappings.update(arc_projection(arc, {i for i in ids if i.startswith('arcpy/')}))
    need(requirements.get('schema') == REQUIREMENTS_SCHEMA_ID and requirements.get('complete') is True,
         'Complete governed release requirements required')
    rows = requirements['requirements']
    requirement_ids = [requirement_id(row) for row in rows]
    need(len(requirement_ids) == len(set(requirement_ids)), 'Duplicate release requirement identity')
    addressable = {key:row for key,row in zip(requirement_ids,rows)
                   if row.get('client_driver') in ('arcpy','pyqgis') and row.get('addressable_by_client') is True}
    # Binding is explicit review data, pinned to these inputs, and never inferred from names.
    links = {}
    if bindings is not None:
        need(bindings.get('schema') == 'honua.native-release-bindings/v1'
             and bindings.get('input_sha256') == input_sha256, 'Reviewed binding input pins differ')
        need(isinstance(bindings.get('review'), str) and bindings['review'].strip(), 'Review reference required')
        for binding in bindings['bindings']:
            key = binding['requirement_id']; refs = binding['cell_ids']
            need(key in addressable and key not in links and isinstance(refs,list) and refs
                 and len(refs) == len(set(refs)) and set(refs) <= ids
                 and all(ref.startswith(addressable[key]['client_driver']+'/') for ref in refs),
                 'Unknown, duplicate, cross-driver or empty release binding')
            links[key] = refs
    arc_unmapped = ({c['cell_id'] for c in arc['cells'] if c['mapping_status'] == 'unmapped'}
                    if arc is not None else set())
    native_backlog=[]
    for cell in cells:
        children = mappings.get(cell['cell_id'], [])
        reasons=[]
        if cell['driver'] == 'arcpy' and arc is None: reasons.append('authoritative-arc-projection-not-supplied')
        elif not children or cell['cell_id'] in arc_unmapped: reasons.append('native-producer-mapping-missing')
        reasons.append('fixture-and-fresh-native-evidence-not-proven')
        if not any(cell['cell_id'] in refs for refs in links.values()): reasons.append('reviewed-release-mapping-missing')
        native_backlog.append({**cell, 'owner': 'honua-esri-compat' if cell['driver']=='arcpy' else 'honua-client-compat',
                               'required_children':children, 'reasons':reasons})
    native_by_id = {cell['cell_id']:cell for cell in native_backlog}
    release_backlog=[]
    for key,row in zip(requirement_ids,rows):
        reasons=[]
        if key in addressable:
            if key not in links:
                reasons.extend(['reviewed-release-mapping-missing', 'native-producer-unresolved-until-reviewed-release-mapping'])
            elif any(any(r in ('native-producer-mapping-missing', 'authoritative-arc-projection-not-supplied')
                         for r in native_by_id[cell]['reasons']) for cell in links[key]):
                reasons.append('linked-native-producer-mapping-missing')
            reasons.append('fixture-and-fresh-native-evidence-not-proven')
        driver = row.get('client_driver')
        owner = ('honua-esri-compat' if driver == 'arcpy' else 'honua-client-compat'
                 if driver == 'pyqgis' else _owned_source_name(row))
        release_backlog.append({'requirement_id':key, 'requirement':row, 'owner':owner,
                                'cell_ids':links.get(key,[]), 'reasons':reasons})
    return native_scope, {'schema':'honua.native-completion-backlog/v1', 'input_sha256':input_sha256,
        'release_requirements_revision':requirements['revision'], 'release_gate_evaluated':False,
        'shipping_certification_credit':False, 'native_required_count':len(cells),
        'native_deferred_count':14, 'release_requirement_count':len(rows),
        'addressable_scripting_requirement_count':len(addressable), 'reviewed_release_mapping_count':len(links),
        'native_cells':native_backlog, 'release_requirements':release_backlog,
        'deferred_cells':[c for c in progress['cells'] if not c['required']]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('scope','licensed-grid','qgis-grid','registry','profile','requirements'):
        parser.add_argument('--'+name, required=True); parser.add_argument('--'+name+'-sha256', required=True)
    for name in ('arc-projection','bindings'):
        parser.add_argument('--'+name); parser.add_argument('--'+name+'-sha256')
    parser.add_argument('--candidate-digest', required=True); parser.add_argument('--integration-source', required=True)
    parser.add_argument('--output', required=True)
    args=parser.parse_args(); data={}; pins={}
    for name in ('scope','licensed_grid','qgis_grid','registry','profile','requirements','arc_projection','bindings'):
        path=getattr(args,name); pin=getattr(args,name+'_sha256')
        need(bool(path) == bool(pin), 'Every optional input requires its SHA256')
        if path: data[name]=captured(path,pin); pins[name]=pin
    # A binding cannot self-pin its own file.
    binding_pins={k:v for k,v in pins.items() if k != 'bindings'}
    native, backlog=build(data['scope'], {'licensed-scripting':data['licensed_grid'], 'qgis-scripting':data['qgis_grid']},
        data['registry'],data['profile'],data['requirements'],args.candidate_digest,args.integration_source,
        data.get('arc_projection'),data.get('bindings'),binding_pins)
    backlog['input_sha256'] = pins
    output=Path(args.output); output.mkdir(parents=True, exist_ok=False)
    for name,value in (('original-scope.json',native),('backlog.json',backlog)):
        with (output/name).open('x',encoding='utf-8',newline='\n') as stream:
            json.dump(value,stream,indent=2,ensure_ascii=False); stream.write('\n')
    print(json.dumps({k:v for k,v in backlog.items() if k.endswith('_count')},sort_keys=True))


if __name__=='__main__': main()
