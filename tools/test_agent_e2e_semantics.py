import copy
import math

import pytest

from agent_e2e_pack import PackError
from agent_e2e_semantics import CASES, SPATIAL_CASES, fixture, verify


def observation(case):
    source=fixture(case)
    if case in CASES[:7]:
        ids = ['s3'] if case in ('select-strict','same-name-isolation') else ['s2','s3'] if case=='select-inclusive' else ['s2','s3','s4']
        result={'srid':3857, 'features':[f for f in source['features'] if f['id'] in ids],
                'pages':[{'sourceIds':[i],'responseDigest':'sha256:'+'a'*64} for i in ids], 'terminalPageObserved':True}
    elif case=='buffer-metric':
        ring=[[10+500*math.cos(2*math.pi*i/64),10+500*math.sin(2*math.pi*i/64)] for i in range(64)]
        ring.append(ring[0])
        result={'srid':3857,'sourceId':'s1','geometry':{'type':'Polygon','coordinates':[ring]}}
    elif case=='transform-coordinates':
        rows=copy.deepcopy(source['features'])
        for row in rows:
            x,y=row['geometry']['coordinates']
            row['geometry']['coordinates']=[math.degrees(x/6378137),math.degrees(2*math.atan(math.exp(y/6378137))-math.pi/2)]
        result={'srid':4326,'features':rows}
    elif case in ('contains-target','contains-join','boundary-hole','join-multiplicity'):
        result={'values':{'contains-target':[['inside','outer'],['inside','overlap']],
            'contains-join':[['outer','inside'],['overlap','inside']], 'boundary-hole':['inside'],
            'join-multiplicity':[{'id':'inside','matches':2},{'id':'outside','matches':0}]}[case]}
    else:
        result={'httpStatus':403 if case=='source-write-denied' else 422,'errorCode':'refused','createdResourceIds':[]}
    return {'sourceBefore':source,'sourceAfter':copy.deepcopy(source),'result':result}


@pytest.mark.parametrize('case',CASES)
def test_semantic_observations_and_source_mutations(case):
    value=observation(case)
    verify(case,value)
    if case in SPATIAL_CASES:
        value['sourceAfter']['pointLayer']['features'][0]['properties']['changed']=True
    else:
        value['sourceAfter']['features'][0]['properties']['capacity']=999
    with pytest.raises(PackError,match='source differs'):
        verify(case,value)


@pytest.mark.parametrize('case',['select-strict','select-inclusive-null','paging-small'])
def test_equal_counts_cannot_hide_substitution(case):
    value=observation(case)
    value['result']['features'][0]=copy.deepcopy(fixture()['features'][0])
    with pytest.raises(PackError,match='membership'):
        verify(case,value)


def test_reversed_contains_and_relabelled_crs_fail():
    value=observation('contains-target')
    value['result']['values']=observation('contains-join')['result']['values']
    with pytest.raises(PackError,match='spatial roles'):
        verify('contains-target',value)
    value=observation('transform-coordinates')
    value['result']['features']=fixture()['features']
    with pytest.raises(PackError,match='relabeled'):
        verify('transform-coordinates',value)


@pytest.mark.parametrize('case', SPATIAL_CASES)
@pytest.mark.parametrize('mutation', ['missing-hole', 'moved-overlap', 'missing-point', 'wrong-crs', 'missing-layer'])
def test_expected_spatial_results_cannot_hide_protected_source_changes(case, mutation):
    value = observation(case)
    source = value['sourceAfter']
    if mutation == 'missing-hole':
        source['polygonLayer']['features'][0]['geometry']['coordinates'].pop()
    elif mutation == 'moved-overlap':
        source['polygonLayer']['features'][1]['geometry']['coordinates'][0][0] = [99, 99]
    elif mutation == 'missing-point':
        source['pointLayer']['features'].pop()
    elif mutation == 'wrong-crs':
        source['srid'] = 4326
    else:
        del source['polygonLayer']
    with pytest.raises(PackError, match='source differs'):
        verify(case, value)


@pytest.mark.parametrize('case', SPATIAL_CASES)
def test_old_point_only_snapshots_do_not_qualify_spatial_checks(case):
    value = observation(case)
    value['sourceBefore'] = value['sourceAfter'] = fixture()
    with pytest.raises(PackError, match='source differs'):
        verify(case, value)
