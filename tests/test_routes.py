import datetime as dt
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import run_routes_weekly as r
import run_weekly as legacy

def test_date_boundaries():
    start,end=dt.date(2026,11,9),dt.date(2026,11,15)
    assert r.legacy.parse_date('2026-11-09')==start
    assert r.iso_date('2026 Nov 14')=='2026-11-14'
    assert start <= r.legacy.parse_date('2026-11-15') <= end
    assert not (start <= r.legacy.parse_date('2026-11-08') <= end)
    assert r.iso_date('2026-11')==''

def test_cross_route_dedupe_retains_labels():
    a={'doi':'10.1000/test','title':'one','sources':['PubMed'],'routes':['C1'],'modules':['C']}
    b={'doi':'10.1000/test','title':'one','sources':['Crossref'],'routes':['C4'],'modules':['C']}
    row=r.merge([a,b])[0]
    assert row['sources']==['Crossref','PubMed']
    assert row['routes']==['C1','C4']

def test_scope_rules():
    cfg={'include_any':['depress'],'require_any':['taste receptor'],'exclude_any':['gambling disorder']}
    assert r.relevant({'title':'Depression and taste receptor','abstract':''},cfg)
    assert not r.relevant({'title':'Depression and gambling disorder taste receptor','abstract':''},cfg)
    assert not r.relevant({'title':'Taste receptor in obesity','abstract':''},cfg)

def test_route_configuration_and_formal_filter():
    routes=legacy.load_yaml(ROOT/'config'/'routes.yml')['routes']
    assert set(routes)=={'A1','A2','A3','B1','B2','B3','C1','C2','C3','C4','D1','D2','D3'}
    for route in routes.values():
        q=route['source_queries']['pubmed']
        assert q.count('(')==q.count(')')
    assert not r.formal_publication({'title':'Correction: a CEBPD study','type':'Published Erratum'})
    assert r.formal_publication({'title':'CEBPD regulates inflammation','type':'Journal Article'})

def test_pubmed_empty_metadata_path(monkeypatch=None):
    # Regression: an empty PubMed summary must become an audited source failure,
    # never crash the complete multi-route weekly run.
    original=r.get_json
    calls=[]
    def fake(url, errors, source, route, tries=2):
        calls.append(url)
        if 'esearch.fcgi' in url:return {'esearchresult':{'count':'1','idlist':['1']}}
        return None
    r.get_json=fake
    try:
        rows,audit=r.pubmed('T','x',dt.date(2026,11,9),dt.date(2026,11,15),1,[])
        assert rows==[] and audit['metadata_fetch_failed'] is True
    finally:r.get_json=original

if __name__=='__main__':
    test_date_boundaries(); test_cross_route_dedupe_retains_labels(); test_scope_rules(); test_route_configuration_and_formal_filter(); test_pubmed_empty_metadata_path(); print('route tests passed')
