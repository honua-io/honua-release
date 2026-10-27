import copy
import pytest
import desktop_qualification as desktop
import native_completion_contract as contract

DIGEST='sha256:'+'a'*64

@pytest.fixture
def source():
    grids={'licensed-scripting':{'candidate':{'digest':DIGEST},'rows':[]},
           'qgis-scripting':{'candidate':{'digest':DIGEST},'rows':[]}}
    for name, driver, count in [('licensed-scripting',desktop.SCRIPTING_DRIVER,477),('qgis-scripting','pyqgis',473)]:
        cells={f'c{i}': {'value':'not-probed'} if name=='licensed-scripting' else {'pyqgis':{'value':'not-probed'}} for i in range(count)}
        row={'row':'surface','cells':cells}
        if name=='licensed-scripting': row['driver']=driver
        grids[name]['rows'].append(row)
    grids['qgis-scripting']['rows'].append({'row':'sensorthings','cells':{
        f'c{i}':{'pyqgis':{'value':'blocked'}} for i in range(14)}})
    scope={'schema':'honua.desktop-scripting-scope/v1','revision':'test','client_limitations':[],'sources':{}}
    for name in grids:
        cells=desktop.normalize(grids[name],name)
        deferred=[] if name=='licensed-scripting' else [{'row':'sensorthings','column':f'c{i}','target_release':'2026.2'} for i in range(14)]
        scope['sources'][name]={'total_count':len(cells),'total_identity_sha256':desktop.identity_digest(cells),
            'applicable_count':len(cells),'applicable_identity_sha256':desktop.identity_digest(cells),
            'required_count':len(cells)-len(deferred),'deferred_preview_cells':deferred}
    profile={'schema':'honua.native-certification-profile/v1','driver':'pyqgis','revision':'p','required_cells':[['surface',f'c{i}','pyqgis'] for i in range(473)]}
    registry={'schema':'honua.native-operation-crosswalk/v1','profile_revision':'p',
        'processes':{'p':{'id':'p','required_case_ids':['assertion'], 'cells':[['surface','c0','pyqgis']]}},
        'cell_requirements':{'surface/c0/pyqgis':['p']},
        'unmapped_cells':[['surface',f'c{i}','pyqgis'] for i in range(1,473)]}
    requirement={'capability_key':'feature','surface':'feature','operation':'query','canonical_client':'QGIS',
        'client_lane':'desktop-pyqgis','client_driver':'pyqgis','addressable_by_client':True}
    requirements={'schema':'honua.protocol-certification-requirements/v1','revision':'r','complete':True,'requirements':[requirement]}
    return scope,grids,registry,profile,requirements


def build(source, **kw):
    return contract.build(*source, DIGEST, 'b'*40, input_sha256={'scope':'hash'}, **kw)


def test_original_scope_retains_all_cells_and_defer_without_fabricating_arc(source):
    scope,backlog=build(source)
    assert len(scope['cells']) == 950 and scope['scope_counts']=={'arcpy':477,'pyqgis':473}
    assert backlog['native_deferred_count']==14 and not backlog['shipping_certification_credit']
    cells={c['cell_id']:c for c in backlog['native_cells']}
    assert cells['pyqgis/surface/c0']['required_children']==[{'case_id':'pyqgis/p','assertion_id':'assertion'}]
    assert 'native-producer-mapping-missing' in cells['pyqgis/surface/c1']['reasons']
    assert 'authoritative-arc-projection-not-supplied' in cells['arcpy/surface/c0']['reasons']
    assert backlog['release_requirements'][0]['reasons'][0]=='reviewed-release-mapping-missing'


def test_explicit_binding_is_exact_pin_and_driver_checked(source):
    key=contract.requirement_id(source[-1]['requirements'][0])
    bindings={'schema':'honua.native-release-bindings/v1','input_sha256':{'scope':'hash'},'review':'owner review',
        'bindings':[{'requirement_id':key,'cell_ids':['pyqgis/surface/c0']}]}
    _,result=build(source,bindings=bindings)
    assert result['reviewed_release_mapping_count']==1 and result['release_requirements'][0]['cell_ids']==['pyqgis/surface/c0']
    bindings['bindings'][0]['cell_ids']=['arcpy/surface/c0']
    with pytest.raises(ValueError,match='cross-driver'): build(source,bindings=bindings)
    bindings['bindings'][0]['cell_ids']=['pyqgis/surface/c0']; bindings['input_sha256']['scope']='other'
    with pytest.raises(ValueError,match='pins differ'): build(source,bindings=bindings)


def test_scope_and_registry_drift_fail_closed(source):
    changed=copy.deepcopy(source); changed[1]['qgis-scripting']['rows'][0]['cells'].pop('c0')
    with pytest.raises(ValueError): build(changed)
    changed=copy.deepcopy(source); changed[2]['cell_requirements']['surface/c0/pyqgis']=['missing']
    with pytest.raises(ValueError,match='process binding'): build(changed)


def test_arc_partial_mapping_remains_backlog(source):
    cells=[{'cell_id':f'arcpy/surface/c{i}', 'mapping_status':'unmapped',
        'required_children':[{'case_id':'features/query','assertion_id':'part'}] if i==0 else []} for i in range(477)]
    arc={'schema':'honua.native-accounting-inventory/v1','lane':'arcpy',
         'cases':[{'case_id':'features/query'}],'cells':cells}
    _,result=build(source,arc=arc)
    item=next(c for c in result['native_cells'] if c['cell_id']=='arcpy/surface/c0')
    assert item['required_children'][0]['assertion_id']=='part'
    assert 'native-producer-mapping-missing' in item['reasons']
    arc['cells'].pop()
    with pytest.raises(ValueError,match='differs from original'): build(source,arc=arc)
