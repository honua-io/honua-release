import copy
import math

import pytest

from agent_e2e_pack import PackError
from agent_e2e_semantics import CASES, fixture, verify


def observation(case):
    source=fixture()
    if case in CASES[:7]:
        ids = ['s3'] if case in ('select-strict','same-name-isolation') else ['s2','s3'] if case=='select-inclusive' else ['s2','s3','s4']
        result={'features':[f for f in source['features'] if f['id'] in ids],
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
