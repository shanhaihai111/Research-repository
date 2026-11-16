#!/usr/bin/env python3
"""Route-based weekly discovery with auditable PubMed, Crossref and OpenAlex checks.

No publication is called a weekly item until a source publication date falls in
the selected window.  Database indexing dates are used only for discovery.
"""
from __future__ import annotations
import argparse, csv, datetime as dt, html, json, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

import run_weekly as legacy

ROOT=Path(__file__).resolve().parents[1]
DATA, REPORTS, LOGS, CONFIG=(ROOT/x for x in ('data','reports','logs','config'))

def get_json(url, errors, source, route, tries=2):
    last=''
    for attempt in range(tries):
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'PsychiatryFrontiersWeekly/2.0 (literature-monitor; mailto:research@example.invalid)'})
            with urllib.request.urlopen(req,timeout=12) as r: return json.load(r)
        except Exception as exc:
            last=f'{type(exc).__name__}: {exc}'; time.sleep(1+attempt)
    errors.append({'route':route,'source':source,'error':last})
    return None

def iso_date(v):
    if isinstance(v,list) and v: v='-'.join(str(x) for x in v[:3])
    s=str(v or '')
    m=re.search(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})',s)
    if m:
        try:return dt.date(*map(int,m.groups())).isoformat()
        except ValueError:return ''
    # PubMed often supplies dates as "2026 Nov 14" rather than ISO strings.
    for fmt in ('%Y %b %d','%Y %B %d'):
        try:return dt.datetime.strptime(re.sub(r'\s+',' ',s.strip()),fmt).date().isoformat()
        except ValueError:pass
    return ''

def pubmed(route, query, start, end, limit, errors):
    # Publication-date and record-entry searches are intentionally independent.
    # The first is the authoritative weekly discovery query; the second only
    # catches delayed indexing and is never itself used as publication date.
    lo=(end-dt.timedelta(days=89)).strftime('%Y/%m/%d'); hi=end.strftime('%Y/%m/%d')
    week=f'({query}) AND ("{start.strftime("%Y/%m/%d")}"[Date - Publication] : "{hi}"[Date - Publication])'
    created=f'({query}) AND ("{lo}"[Date - Create] : "{hi}"[Date - Create])'
    def search(q):
        raw=get_json('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','term':q,'retmax':limit,'retmode':'json','sort':'pub date'}),errors,'PubMed',route,2)
        x=(raw or {}).get('esearchresult',{}); return x.get('idlist',[]),int(x.get('count','0'))
    week_ids,week_count=search(week); create_ids,create_count=search(created)
    ids=list(dict.fromkeys(week_ids+create_ids))
    if not ids:return [],{'publication_date_query':week,'entry_date_query':created,'returned_publication_date':week_count,'returned_entry_date':create_count,'retrieved':0}
    summ=get_json('https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?'+urllib.parse.urlencode({'db':'pubmed','id':','.join(ids),'retmode':'json'}),errors,'PubMed',route,2)
    if not summ:
        return [],{'publication_date_query':week,'entry_date_query':created,
                   'returned_publication_date':week_count,'returned_entry_date':create_count,
                   'retrieved':0,'metadata_fetch_failed':True}
    absmap={}
    try:
        ef='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','id':','.join(ids),'retmode':'xml'})
        xml=urllib.request.urlopen(urllib.request.Request(ef,headers={'User-Agent':'PsychiatryFrontiersWeekly/2.0'}),timeout=12).read()
        for art in ET.fromstring(xml).findall('.//PubmedArticle'):
            p=art.findtext('.//PMID') or ''; absmap[p]=' '.join(''.join(n.itertext()).strip() for n in art.findall('.//Abstract/AbstractText')).strip()
    except Exception as exc: errors.append({'route':route,'source':'PubMed EFetch','error':str(exc)})
    rows=[]
    for p in ids:
        x=summ.get('result',{}).get(p,{})
        ident={i.get('idtype',''):i.get('value','') for i in x.get('articleids',[]) if isinstance(i,dict)}
        epub=iso_date(x.get('epubdate')); printdate=iso_date(x.get('pubdate'))
        rows.append({'pmid':p,'doi':ident.get('doi',''),'title':x.get('title','').rstrip('.'),'journal':x.get('fulljournalname') or x.get('source',''),'online_date':epub or printdate,'date_sources':{'pubmed_epub':epub,'pubmed_print':printdate},'abstract':absmap.get(p,''),'type':','.join(x.get('pubtype',[])[:4]) or '待人工判定','publisher_url':f'https://pubmed.ncbi.nlm.nih.gov/{p}/','sources':['PubMed'],'evidence':'PubMed 元数据及 EFetch 摘要核验。'})
    return rows,{'publication_date_query':week,'entry_date_query':created,'returned_publication_date':week_count,'returned_entry_date':create_count,'retrieved':len(rows)}

def crossref(route, query, start, end, limit, errors):
    params={'query.bibliographic':query,'filter':f'from-pub-date:{(end-dt.timedelta(days=89)).isoformat()},until-pub-date:{end.isoformat()},type:journal-article','rows':limit,'select':'DOI,title,container-title,published-online,published-print,published,abstract,URL,type'}
    raw=get_json('https://api.crossref.org/works?'+urllib.parse.urlencode(params),errors,'Crossref',route)
    if not raw:return [],{'returned':0,'error':True}
    items=raw.get('message',{}).get('items',[]); rows=[]
    for x in items:
        online=iso_date(x.get('published-online',{}).get('date-parts',[[]])[0]); pr=iso_date(x.get('published-print',{}).get('date-parts',[[]])[0]); pub=iso_date(x.get('published',{}).get('date-parts',[[]])[0])
        rows.append({'pmid':'','doi':x.get('DOI',''),'title':(x.get('title')or[''])[0].rstrip('.'),'journal':(x.get('container-title')or[''])[0],'online_date':online or pr or pub,'date_sources':{'crossref_online':online,'crossref_print':pr,'crossref_published':pub},'abstract':re.sub('<[^>]+>',' ',x.get('abstract','')),'type':x.get('type','journal-article'),'publisher_url':x.get('URL',''),'sources':['Crossref'],'evidence':'Crossref DOI/元数据核验；摘要字段可用时仅作候选筛选。'})
    return rows,{'query':query,'returned':len(items),'retrieved':len(rows)}

def openalex(route, query, start, end, limit, errors):
    params={'search':query,'filter':f'from_publication_date:{(end-dt.timedelta(days=89)).isoformat()},to_publication_date:{end.isoformat()},type:article','per-page':limit}
    raw=get_json('https://api.openalex.org/works?'+urllib.parse.urlencode(params),errors,'OpenAlex',route)
    if not raw:return [],{'returned':0,'error':True}
    rows=[]
    for x in raw.get('results',[]):
        inv=x.get('abstract_inverted_index') or {}; words=[]
        if inv:
            pos={p:w for w,ps in inv.items() for p in ps}; words=[pos[i] for i in range(max(pos)+1) if i in pos]
        doi=(x.get('doi')or'').replace('https://doi.org/','')
        rows.append({'pmid':'','doi':doi,'title':x.get('title','').rstrip('.'),'journal':((x.get('primary_location')or{}).get('source')or{}).get('display_name',''),'online_date':iso_date(x.get('publication_date')),'date_sources':{'openalex_publication':iso_date(x.get('publication_date'))},'abstract':' '.join(words),'type':x.get('type','article'),'publisher_url':x.get('doi') or x.get('id',''),'sources':['OpenAlex'],'evidence':'OpenAlex 元数据用于交叉补检；出版日期需与 DOI/期刊页复核。'})
    return rows,{'query':query,'returned':raw.get('meta',{}).get('count',len(rows)),'retrieved':len(rows)}

def key(a): return (a.get('doi')or'').lower() or ('pmid:'+a['pmid'] if a.get('pmid') else legacy.norm_title(a.get('title','')))
def merge(rows):
    out={}
    for a in rows:
        k=key(a)
        if not k:continue
        if k not in out:out[k]=a
        else:
            b=out[k]; b['sources']=sorted(set(b.get('sources',[])+a.get('sources',[])))
            b['routes']=sorted(set(b.get('routes',[])+a.get('routes',[])))
            b['modules']=sorted(set(b.get('modules',[])+a.get('modules',[])))
            b['date_sources']={**b.get('date_sources',{}),**a.get('date_sources',{})}
            if not b.get('abstract') and a.get('abstract'): b['abstract']=a['abstract']
            if not b.get('online_date') and a.get('online_date'): b['online_date']=a['online_date']
            if not b.get('pmid') and a.get('pmid'):b['pmid']=a['pmid']
    return list(out.values())

def relevant(a,cfg):
    text=(a.get('title','')+' '+a.get('abstract','')).lower()
    inc=cfg.get('include_any',[]); req=cfg.get('require_any',[]); exc=cfg.get('exclude_any',[])
    return (not inc or any(x.lower() in text for x in inc)) and (not req or any(x.lower() in text for x in req)) and not any(x.lower() in text for x in exc)

def formal_publication(a):
    """Conservative separation of formal articles from preprints/corrections."""
    kind=(a.get('type','')+' '+a.get('title','')).lower()
    return not any(x in kind for x in ('preprint','published erratum','correction:','correction to:'))

def route_records(code,cfg,start,end,limit,errors,sources=('pubmed','crossref','openalex')):
    queries=cfg['source_queries']
    p,pa=(pubmed(code,queries['pubmed'],start,end,limit,errors) if 'pubmed' in sources else ([],{'skipped':True}))
    c,ca=(crossref(code,queries['crossref'],start,end,limit,errors) if 'crossref' in sources else ([],{'skipped':True}))
    o,oa=(openalex(code,queries['openalex'],start,end,limit,errors) if 'openalex' in sources else ([],{'skipped':True}))
    unique=merge(p+c+o); dated=[a for a in unique if legacy.parse_date(a.get('online_date'))]; pending=[a for a in unique if not legacy.parse_date(a.get('online_date'))]
    weekly=[a for a in dated if start<=legacy.parse_date(a['online_date'])<=end]; topical=[a for a in weekly if relevant(a,cfg)]; formal=[a for a in topical if formal_publication(a)]
    for a in formal:
        a['routes']=[code]; a['modules']=[cfg['section']]; a['direct_evidence']=cfg.get('direct_evidence',True)
    audit={'route':code,'title':cfg['title'],'queries':{'PubMed':pa,'Crossref':ca,'OpenAlex':oa},'source_candidates':{'PubMed':len(p),'Crossref':len(c),'OpenAlex':len(o)},'merged_unique':len(unique),'date_resolved':len(dated),'date_pending':len(pending),'target_week':len(weekly),'topic_relevant':len(topical),'formal_peer_review_candidates':len(formal),'final_display_before_crossroute_dedupe':min(len(formal),cfg.get('display_limit',8)),'exclusions':{'outside_target_week_or_delayed':len(dated)-len(weekly),'date_unresolved':len(pending),'topic_or_scope':len(weekly)-len(topical),'preprint_or_correction':len(topical)-len(formal)}}
    return formal[:cfg.get('display_limit',8)],audit,unique[:10]

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--date',default=dt.date.today().isoformat(),help='Monday after the target natural week'); ap.add_argument('--no-email',action='store_true'); ap.add_argument('--send-email',action='store_true'); ap.add_argument('--confirm-send',action='store_true'); ap.add_argument('--diagnostic-only',action='store_true'); ap.add_argument('--routes',default='',help='Comma-separated route codes for a bounded diagnostic run'); ap.add_argument('--per-route',type=int,default=250); ap.add_argument('--sources',default='pubmed,crossref,openalex'); ap.add_argument('--test-email',action='store_true',help='prepare an explicit test email; requires --confirm-test-email to send'); ap.add_argument('--confirm-test-email',action='store_true',help='explicit approval to submit the test email'); args=ap.parse_args()
    endday=dt.date.fromisoformat(args.date)-dt.timedelta(days=1); start=endday-dt.timedelta(days=6)
    routes=legacy.load_yaml(CONFIG/'routes.yml')['routes'];
    if args.routes:
        wanted={x.strip() for x in args.routes.split(',') if x.strip()}; routes={k:v for k,v in routes.items() if k in wanted}
    errors=[]; audits=[]; allrows=[]; samples={}
    # Four concurrent routes keep the run practical while avoiding an uncontrolled
    # burst to any one scholarly API.
    with ThreadPoolExecutor(max_workers=4) as pool:
        source_set=tuple(x.strip().lower() for x in args.sources.split(',') if x.strip())
        futures={pool.submit(route_records,code,cfg,start,endday,args.per_route,errors,source_set):code for code,cfg in routes.items()}
        for future in as_completed(futures):
            code=futures[future]; rows,audit,example=future.result(); allrows.extend(rows); audits.append(audit); samples[code]=example
    audits.sort(key=lambda x:x['route'])
    unique=merge(allrows); history=legacy.load_history(); seen={key(x) for x in history}; final=[]
    for a in unique:
        if key(a) in seen:continue
        final.append(a)
    # Route membership must survive cross-source consolidation.
    for a in final:
        a['route_titles']=[routes[r]['title'] for r in a.get('routes',[])]; legacy.metrics_lookup([a])
    out=REPORTS/args.date; out.mkdir(parents=True,exist_ok=True); LOGS.mkdir(exist_ok=True)
    audit={'run_date':args.date,'coverage_start':start.isoformat(),'coverage_end':endday.isoformat(),'routes':audits,'cross_route_unique':len(unique),'history_duplicates':len(unique)-len(final),'final_unique':len(final),'api_errors':errors,'test_scope':'不发送邮件；该次为本地诊断/预览运行。'}
    (out/'search_audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'candidate_examples.json').write_text(json.dumps(samples,ensure_ascii=False,indent=2),encoding='utf-8')
    with (out/'included_records.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['routes','title','doi','pmid','journal','online_date','sources','type','evidence']; w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();[w.writerow({**a,'routes':';'.join(a.get('routes',[])),'sources':';'.join(a.get('sources',[]))}) for a in final]
    # One primary A-D placement per paper prevents double counting; all route labels
    # remain in the card and audit for transparent cross-listing.
    section_topics={s:{'title':s+'　'+name,'color':color} for s,name,color in [('A','抑郁症与游戏成瘾','#22577A'),('B','味觉受体、味觉感知与疾病','#8C5A1E'),('C','脑屏障、脉络丛、CEBPD与神经炎症','#7A3E65'),('D','人脑意识科学','#3D5A80')]}
    for a in final:
        a['modules']=[a.get('modules',['A'])[0]]
        a['route_label']='；'.join(a.get('route_titles',[]))
    legacy_audit={'candidates':sum(x['merged_unique'] for x in audits),'duplicates':len(unique)-len(final),'out_of_window':sum(x['exclusions']['outside_target_week_or_delayed'] for x in audits),'api_errors':len(errors)}
    (out/f'Weekly_Research_Report_{dt.date.fromisoformat(args.date):%Y%m%d}.html').write_text(legacy.make_html(final,section_topics,start,endday,args.date,legacy_audit),encoding='utf-8')
    legacy.make_pdf(final,section_topics,start,endday,args.date,out/f'Weekly_Research_Report_{dt.date.fromisoformat(args.date):%Y%m%d}.pdf')
    email_status='PREVIEW_ONLY'
    send_requested=(args.send_email and args.confirm_send) or (args.test_email and args.confirm_test_email)
    if not args.no_email and not args.diagnostic_only:
        date_key=dt.date.fromisoformat(args.date).strftime('%Y%m%d')
        prior_log=LOGS/f'route_run_{args.date}.json'
        from email_delivery import previously_attempted
        if send_requested and not args.test_email and previously_attempted(prior_log):
            email_status='PREVIEW_ONLY'
            (out/'email_delivery.json').write_text(json.dumps({
                'status':'PREVIEW_ONLY','reason':'duplicate_send_blocked',
                'report_date':args.date,'smtp_submission':'not_attempted',
                'final_delivery_confirmed':False
            },ensure_ascii=False,indent=2),encoding='utf-8')
            print('Duplicate send blocked for an already-attempted report date.')
        else:
            email_status=legacy.email_preview(
                out,date_key,final,start,endday,
                send_requested,send_requested,test_mode=args.test_email
            )
    if final and not args.diagnostic_only:
        with (DATA/'history.jsonl').open('a',encoding='utf-8') as f:
            for a in final:f.write(json.dumps({**a,'first_found':args.date,'report_week':args.date},ensure_ascii=False)+'\n')
    (LOGS/f'route_run_{args.date}.json').write_text(json.dumps({**audit,'email_status':email_status},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'coverage':[start.isoformat(),endday.isoformat()],'routes':len(routes),'final_unique':len(final),'api_errors':len(errors),'email':email_status,'audit':str(out/'search_audit.json')},ensure_ascii=False))
if __name__=='__main__':main()
