#!/usr/bin/env python3
"""Reproducible weekly literature-monitoring pipeline (standard-library first)."""
from __future__ import annotations
import argparse, csv, datetime as dt, email.message, hashlib, html, json, os, re, smtplib, ssl, sys, textwrap, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DATA, REPORTS, LOGS, CONFIG = (ROOT / x for x in ('data','reports','logs','config'))

def load_yaml(path: Path):
    # PyYAML is bundled in the intended runtime; fail loudly rather than silently using malformed config.
    import yaml
    return yaml.safe_load(path.read_text(encoding='utf-8'))

def norm_title(s): return re.sub(r'[^a-z0-9]+', '', (s or '').lower())
def article_id(a): return a.get('doi','').lower() or ('pmid:'+a['pmid'] if a.get('pmid') else norm_title(a.get('title','')))
def monday_window(report_date: dt.date):
    # Report runs Monday; analyse immediately preceding natural week Monday-Sunday.
    anchor = report_date - dt.timedelta(days=report_date.weekday())
    return anchor-dt.timedelta(days=7), anchor-dt.timedelta(days=1)
def parse_date(s):
    try: return dt.date.fromisoformat((s or '')[:10])
    except ValueError: return None

def fetch_json(url, tries=3):
    last=None
    for n in range(tries):
        try:
            req=urllib.request.Request(url, headers={'User-Agent':'PsychiatryFrontiersWeekly/1.0 (research-literature-monitor)'})
            with urllib.request.urlopen(req, timeout=25) as r: return json.load(r)
        except Exception as e:
            last=e; time.sleep(2**n)
    raise RuntimeError(f'network query failed after {tries} attempts: {last}')

def pubmed_search(query, retmax=80):
    base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?'
    p=urllib.parse.urlencode({'db':'pubmed','term':query,'retmax':retmax,'retmode':'json','sort':'pub date'})
    return fetch_json(base+p)['esearchresult'].get('idlist',[])

def pubmed_summary(pmids):
    if not pmids: return []
    base='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?'
    raw=fetch_json(base+urllib.parse.urlencode({'db':'pubmed','id':','.join(pmids),'retmode':'json'}))['result']
    abstract_map={}
    # EFetch supplies source abstracts. Parsing XML rather than an index snippet
    # prevents the report from inventing content when an abstract is unavailable.
    try:
        u='https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?'+urllib.parse.urlencode({'db':'pubmed','id':','.join(pmids),'retmode':'xml'})
        xml=urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'PsychiatryFrontiersWeekly/1.0'}),timeout=25).read()
        for art in ET.fromstring(xml).findall('.//PubmedArticle'):
            pid=art.findtext('.//PMID') or ''
            pieces=[]
            for node in art.findall('.//Abstract/AbstractText'):
                label=node.attrib.get('Label','').strip()
                val=''.join(node.itertext()).strip()
                if val: pieces.append((label+': ' if label else '')+val)
            abstract_map[pid]=' '.join(pieces)
    except Exception:
        pass
    rows=[]
    for p in pmids:
        x=raw.get(p,{})
        ids={v.get('idtype',''):v.get('value','') for v in x.get('articleids',[]) if isinstance(v,dict)}
        date=(x.get('epubdate') or x.get('pubdate') or '')
        # PubMed may give only YYYY Mon; retain but do not classify into weekly issue until exact date is available.
        m=re.search(r'(\d{4})[- ]([A-Za-z]{3}|\d{1,2})[- ]?(\d{1,2})?', date)
        iso=''
        if m:
            try:
                mon = int(m.group(2)) if m.group(2).isdigit() else dt.datetime.strptime(m.group(2),'%b').month
                iso=dt.date(int(m.group(1)),mon,int(m.group(3) or 1)).isoformat()
            except ValueError: pass
        ab=abstract_map.get(p,'')
        rows.append({'pmid':p,'doi':ids.get('doi',''),'title':x.get('title','').rstrip('.'),'journal':x.get('fulljournalname') or x.get('source',''),'online_date':iso,'type':','.join(x.get('pubtype',[])[:3]) or '待人工判定','modules':[],'abstract':ab,'publisher_url':f'https://pubmed.ncbi.nlm.nih.gov/{p}/','evidence':'PubMed PMID/元数据核验；'+('摘要来自 PubMed EFetch。' if ab else '未取得摘要，禁止结果解释。')})
    return rows

def classify(a, topics):
    blob=(a.get('title','')+' '+a.get('abstract','')).lower()
    got=[]
    for key,t in topics.items():
        if any(k.lower() in blob for k in t.get('include',[])) and not any(k.lower() in blob for k in t.get('exclude',[])):
            got.append(key)
    a['modules']=got
    return a

def load_history():
    p=DATA/'history.jsonl'
    if not p.exists(): return []
    return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]

def dedupe(records, existing):
    seen={article_id(x) for x in existing}; out=[]; duplicates=[]
    for a in records:
        k=article_id(a)
        if not k or k in seen: duplicates.append(a); continue
        seen.add(k); out.append(a)
    return out,duplicates

def metrics_lookup(records):
    metrics={}
    p=DATA/'journal_metrics.csv'
    if p.exists():
        for r in csv.DictReader(p.open(encoding='utf-8-sig')):
            if r.get('journal'): metrics[r['journal'].strip().lower()]=r
    for a in records:
        m=metrics.get(a.get('journal','').lower())
        a['cas_2025_major']=m.get('cas_2025_major','未核验') if m else '未核验'
        a['cas_2025_minor']=m.get('cas_2025_minor','未核验') if m else '未核验'
        a['impact_factor']=m.get('impact_factor','未核验') if m else '未核验'
        a['metrics_source']=m.get('source','未导入授权指标表') if m else '未导入授权指标表'
    return records

def brief(a):
    if a.get('summary_cn'): return a['summary_cn']
    ab=' '.join((a.get('abstract') or '').split())
    if not ab: return '仅完成元数据核验；未取得摘要，不对研究结果作解释。'
    # conservative, traceable abstract compression
    sentences=re.split(r'(?<=[.!?])\s+',ab)
    return ' '.join(sentences[:2])[:420]

def translation(a):
    return a.get('title_cn','题名中文译文待人工核校。')

def detailed_cn(a):
    if a.get('detail_cn'): return a['detail_cn']
    if not a.get('abstract'):
        return '尚未获得摘要；本期不对研究结果作延伸解释。'
    return '本解读仅依据 PubMed 摘要：'+brief(a)

def weekly_takeaway(records):
    """Concise Chinese synthesis. Specific editorial text is supplied with verified seed records;
    live papers fall back to their validated Chinese summaries without inventing results."""
    supplied=[a.get('weekly_takeaway_cn','') for a in records if a.get('weekly_takeaway_cn')]
    if supplied: return supplied[0][:250]
    if not records: return '本期未纳入符合条件的新论文。'
    summaries='；'.join(brief(a) for a in records[:3])
    return f'本期纳入 {len(records)} 项研究。{summaries}'[:250]

def related_note(a):
    b=(a.get('title','')+' '+a.get('abstract','')).lower()
    if 'tas2r' in b or 'bitter' in b: return '与味觉受体方向相关；需区分受体分子功能与主观味觉感知，并核对物种基因同源关系。'
    if 'choroid plexus' in b: return '与脉络丛/血液-脑脊液屏障相关；不能外推为血脑屏障因果证据。'
    if 'cebpd' in b or 'c/ebp' in b: return '涉及 CEBPD 轴；建议优先核对细胞类型、上游刺激和因果干预。'
    if 'depression' in b: return '对抑郁症研究具有直接或临床关联价值；须按研究设计判断因果强度。'
    return '与既有重点方向无直接机制连接；不强行外推。'

def make_html(records, topics, start, end, report_day, audit):
    css='''*{box-sizing:border-box}body{margin:0;background:#f6f7f8;color:#1c2b36;font-family:"Noto Sans CJK SC","Microsoft YaHei",Arial,sans-serif;line-height:1.68}.wrap{max-width:1120px;margin:auto;padding:42px 24px 72px}header{border-bottom:1px solid #d9e0e5;padding-bottom:25px}h1{font-family:"Times New Roman","Noto Serif CJK SC",serif;font-size:32px;line-height:1.25;margin:0 0 6px;color:#13283b}.sub{font-family:"Times New Roman",serif;color:#607180;font-size:15px}.meta{margin-top:18px;color:#526577;font-size:14px}.nav{position:sticky;top:0;background:#f6f7f8ee;backdrop-filter:blur(8px);padding:12px 0;z-index:2;border-bottom:1px solid #dce3ea}.nav button{border:1px solid #c9d5df;background:#fff;border-radius:16px;padding:5px 11px;margin:3px;color:#31475b;cursor:pointer}.tools{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0}.tools input,.tools select{padding:9px 10px;border:1px solid #cad5df;border-radius:6px;background:#fff}.section{margin-top:44px}.section h2{font-size:23px;margin-bottom:8px}.empty{color:#637586;font-style:italic}.card{background:var(--bg);border:1px solid #dbe3e9;border-left:5px solid var(--c);border-radius:8px;margin:16px 0;padding:18px 20px;box-shadow:0 1px 1px #0b20300a}.card h3{font-family:"Times New Roman",serif;font-size:18px;line-height:1.35;margin:0 0 4px}.translation{font-family:"Noto Serif CJK SC","Songti SC",serif;font-size:13px;color:#647482;margin:0 0 10px}.badge{font-size:12px;border-radius:12px;background:#eef2f4;padding:3px 8px;margin-right:5px}.detail{display:none;border-top:1px solid #dce5e9;margin-top:14px;padding-top:14px}.card.open .detail{display:block}.card button{border:0;background:transparent;color:#1a5e86;padding:0;font-weight:600;cursor:pointer}.small{font-size:13px;color:#607180}.summary{background:#eef3f5;border:1px solid #d5e0e5;border-radius:8px;padding:16px 20px;margin:24px 0}.link{color:#125d8b;text-decoration:none}@media(max-width:620px){.wrap{padding:26px 15px}.tools{display:block}.tools>*{width:100%;margin:4px 0}h1{font-size:26px}}'''
    nav=''.join(f'<button onclick="document.getElementById(\'{k}\').scrollIntoView()">{html.escape(v["title"].split(" ",1)[-1])}</button>' for k,v in topics.items())
    cards=[]
    by=defaultdict(list)
    for a in records:
        for m in a['modules']: by[m].append(a)
    for key,t in topics.items():
        rows=[]
        for i,a in enumerate(by[key]):
            links=[]
            if a.get('doi'): links.append(f'<a class="link" href="https://doi.org/{html.escape(a["doi"])}" target="_blank">DOI</a>')
            if a.get('pmid'): links.append(f'<a class="link" href="https://pubmed.ncbi.nlm.nih.gov/{a["pmid"]}/" target="_blank">PubMed</a>')
            importance=3 if ('随机' in a['type'] or 'Randomized' in a['type']) else 2
            bg=('#f9fbfb','#f6f9fc','#faf8f5')[i%3]
            route_line=(f' · 检索路线：{html.escape(a.get("route_label",""))}' if a.get('route_label') else '')
            rows.append(f'''<article class="card" data-module="{key}" data-type="{html.escape(a['type']).lower()}" data-date="{html.escape(a['online_date'] or '0000-00-00')}" data-importance="{importance}" style="--c:{t['color']};--bg:{bg}"><h3>{html.escape(a['title'])}</h3><p class="translation">{html.escape(translation(a))}</p><div class="small">{html.escape(a['journal'])} · 首次在线：{html.escape(a['online_date'] or '待核验')}{route_line} · {' · '.join(links)}</div><p>{html.escape(brief(a))}</p><button onclick="this.closest('.card').classList.toggle('open')">展开详细解读</button><div class="detail"><p><b>研究设计、主要发现与局限：</b>{html.escape(detailed_cn(a))}</p><p><b>科研价值：</b>{html.escape(related_note(a))}</p><p class="small">研究类型：{html.escape(a['type'])} ｜ PMID {html.escape(a.get('pmid','无'))} ｜ DOI {html.escape(a.get('doi','无'))} ｜核验日 {report_day}</p></div></article>''')
        body=''.join(rows) or '<p class="empty">本周未检索到符合纳入条件的新论文。</p>'
        cards.append(f'<section class="section" id="{key}"><h2 style="color:{t["color"]}">{html.escape(t["title"])}</h2>{body}</section>')
    counts=' · '.join(f'{topics[k]["title"].split(" ",1)[-1]} {len(by[k])} 篇' for k in topics)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>精神医学与意识科学前沿周报 {report_day}</title><style>{css}</style></head><body><main class="wrap"><header><h1>精神医学与意识科学前沿研究周报</h1><div class="sub">Weekly Frontiers in Psychiatry, Taste Biology and Consciousness</div><div class="meta">覆盖期：{start} 至 {end} ｜生成日：{report_day} ｜正式纳入：{len(records)} 篇 ｜{counts}</div></header><nav class="nav">{nav}</nav><div class="summary"><b>本期要点。</b> {html.escape(weekly_takeaway(records))}</div><div class="tools"><input id="q" placeholder="搜索标题、期刊、关键词" oninput="filterCards()"><select id="mod" onchange="filterCards()"><option value="">全部栏目</option>{''.join(f'<option value="{k}">{html.escape(v["title"])}</option>' for k,v in topics.items())}</select><select id="typ" onchange="filterCards()"><option value="">全部论文类型</option><option value="随机">随机试验</option><option value="队列">队列/观察性</option><option value="机制">机制研究</option></select><select id="sort" onchange="sortCards()"><option value="importance">按重要性</option><option value="date">按发表日期</option><option value="title">按标题</option></select></div>{''.join(cards)}<footer class="small">检索与核验记录见同目录 audit.json。</footer></main><script>function filterCards(){{let q=document.getElementById('q').value.toLowerCase(),m=document.getElementById('mod').value,t=document.getElementById('typ').value.toLowerCase();document.querySelectorAll('.card').forEach(c=>c.style.display=(!q||c.innerText.toLowerCase().includes(q))&&(!m||c.dataset.module===m)&&(!t||c.dataset.type.includes(t))?'block':'none')}}function sortCards(){{let k=document.getElementById('sort').value;document.querySelectorAll('.section').forEach(s=>{{let x=[...s.querySelectorAll('.card')];x.sort((a,b)=>{{if(k==='title')return a.innerText.localeCompare(b.innerText);if(k==='date')return b.dataset.date.localeCompare(a.dataset.date);return b.dataset.importance-a.dataset.importance}});x.forEach(c=>s.appendChild(c))}})}}sortCards();</script></body></html>'''

def font_path():
    target=ROOT/'assets'/'NotoSansCJKSC-Regular.ttf'
    if target.exists(): return target
    try:
        from fontTools.ttLib import TTFont
        src='/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
        f=TTFont(src,fontNumber=2); f.flavor=None; f.save(target); return target
    except Exception: return None

def make_pdf(records, topics, start, end, report_day, outfile):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer, PageBreak, NextPageTemplate, Table, TableStyle, KeepTogether
    chinese='STSong-Light'
    try: pdfmetrics.registerFont(UnicodeCIDFont(chinese))
    except Exception: chinese='Helvetica'
    styles=getSampleStyleSheet()
    title=ParagraphStyle('title',parent=styles['Title'],fontName=chinese,fontSize=20,leading=27,textColor=colors.HexColor('#122d40'),spaceAfter=6)
    english=ParagraphStyle('english',parent=styles['BodyText'],fontName='Times-Roman',fontSize=9,leading=12,textColor=colors.HexColor('#5c6b76'))
    h=ParagraphStyle('h',parent=styles['Heading2'],fontName=chinese,fontSize=13,leading=18,spaceBefore=8,spaceAfter=6,textColor=colors.HexColor('#173e57'))
    body=ParagraphStyle('b',parent=styles['BodyText'],fontName=chinese,fontSize=8.4,leading=13.1,spaceAfter=5)
    small=ParagraphStyle('s',parent=body,fontSize=7.2,leading=10.5,textColor=colors.HexColor('#566b79'))
    etitle=ParagraphStyle('et',parent=english,fontName='Times-Bold',fontSize=9.2,leading=12,textColor=colors.HexColor('#173e57'),spaceAfter=2)
    trans=ParagraphStyle('trans',parent=body,fontName=chinese,fontSize=8.1,leading=11.6,textColor=colors.HexColor('#596d7a'),spaceAfter=5)
    page_w,page_h=A4; left=16*mm; right=16*mm; top=15*mm; bottom=16*mm; gutter=7*mm
    full=Frame(left,bottom,page_w-left-right,page_h-top-bottom,id='full')
    colw=(page_w-left-right-gutter)/2
    cols=[Frame(left,bottom,colw,page_h-top-bottom,id='left'),Frame(left+colw+gutter,bottom,colw,page_h-top-bottom,id='right')]
    def footer(canvas,doc):
        canvas.saveState(); canvas.setStrokeColor(colors.HexColor('#dbe3e7')); canvas.line(left,12*mm,page_w-right,12*mm)
        canvas.setFont(chinese,7); canvas.setFillColor(colors.HexColor('#627483')); canvas.drawString(left,7.5*mm,'精神医学与意识科学前沿研究周报'); canvas.drawRightString(page_w-right,7.5*mm,str(doc.page)); canvas.restoreState()
    doc=BaseDocTemplate(str(outfile),pagesize=A4,leftMargin=left,rightMargin=right,topMargin=top,bottomMargin=bottom,title='Weekly Frontiers Report')
    doc.addPageTemplates([PageTemplate(id='cover',frames=[full],onPage=footer),PageTemplate(id='two',frames=cols,onPage=footer)])
    story=[Paragraph('精神医学与意识科学前沿研究周报',title),Paragraph('Weekly Frontiers in Psychiatry, Taste Biology and Consciousness',english),Spacer(1,12),Paragraph(f'覆盖期：{start} 至 {end}　｜　生成日：{report_day}　｜　正式纳入：{len(records)} 篇',small),Spacer(1,12),Paragraph('本期要点',h),Paragraph(weekly_takeaway(records),body),Spacer(1,10)]
    overview=[]
    for key,t in topics.items(): overview.append(Paragraph(f'<font color="{t["color"]}">●</font> {t["title"]}　{sum(key in a["modules"] for a in records)} 篇',body))
    story += [Table([[x] for x in overview],colWidths=[page_w-left-right],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#eef3f5')),('BOX',(0,0),(-1,-1),0.4,colors.HexColor('#d5e0e5')),('LEFTPADDING',(0,0),(-1,-1),10),('RIGHTPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)])),NextPageTemplate('two'),PageBreak()]
    for key,t in topics.items():
        story += [Paragraph(t['title'],h)]
        rs=[a for a in records if key in a['modules']]
        if not rs: story.append(Paragraph('本周未检索到符合纳入条件的新论文。',body)); continue
        for i,a in enumerate(rs):
            content=[Paragraph(a['title'],etitle),Paragraph(translation(a),trans),Paragraph(f'{a["journal"]} ｜首次在线：{a["online_date"]} ｜{a["type"]}',small),Paragraph('<b>研究提炼：</b>'+html.escape(brief(a)),body),Paragraph('<b>详细解读：</b>'+html.escape(detailed_cn(a)),body),Paragraph('<b>科研价值：</b>'+html.escape(related_note(a)),body)]
            if a.get('doi'): content.append(Paragraph(f'<link href="https://doi.org/{a["doi"]}">DOI: {a["doi"]}</link>',small))
            if a.get('pmid'): content.append(Paragraph(f'<link href="https://pubmed.ncbi.nlm.nih.gov/{a["pmid"]}/">PubMed: {a["pmid"]}</link>',small))
            shade=[colors.HexColor('#f5f9fa'),colors.HexColor('#f7f8fc'),colors.HexColor('#faf8f5')][i%3]
            card=Table([[content]],colWidths=[colw],style=TableStyle([('BACKGROUND',(0,0),(-1,-1),shade),('BOX',(0,0),(-1,-1),0.45,colors.HexColor('#d8e2e6')),('LINEBEFORE',(0,0),(0,-1),3,colors.HexColor(t['color'])),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
            story += [KeepTogether([card,Spacer(1,7)])]
    doc.build(story)

def load_env(path):
    if not path.exists(): return {}
    return dict(line.strip().split('=',1) for line in path.read_text().splitlines() if line.strip() and not line.startswith('#') and '=' in line)
def email_preview(report_dir, date_str, records, start, end, send, confirm, test_mode=False):
    from email_delivery import deliver_report
    env=load_env(CONFIG/'email.env')
    result=deliver_report(
        report_dir=report_dir,
        date_str=date_str,
        start=str(start),
        end=str(end),
        count=len(records),
        env=env,
        send=send,
        confirm=confirm,
        test_mode=test_mode,
    )
    return result['status']

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--date',default=dt.date.today().isoformat()); ap.add_argument('--sample',action='store_true'); ap.add_argument('--no-email',action='store_true'); ap.add_argument('--send-email',action='store_true'); ap.add_argument('--confirm-send',action='store_true'); args=ap.parse_args()
    day=dt.date.fromisoformat(args.date); start,end=monday_window(day); cfg=load_yaml(CONFIG/'topics.yml'); topics=cfg['topics']; history=load_history(); failures=[]
    LOGS.mkdir(parents=True,exist_ok=True)
    candidates=[]
    if args.sample:
        candidates=json.loads((DATA/'sample_verified_records.json').read_text(encoding='utf-8'))
    else:
        for key,t in topics.items():
            try:
                rows=pubmed_summary(pubmed_search(t['query']))
                for a in rows: a['modules']=[key]
                candidates.extend(rows)
            except Exception as e: failures.append({'module':key,'error':str(e)})
    for a in candidates:
        if not a.get('modules'): classify(a,topics)
    fresh,duplicates=dedupe(candidates,history)
    # A manually re-run issue must reproduce the same report rather than replace it
    # with an empty one. Prior reports from *other* weeks remain de-duplicated.
    current_ids={article_id(x) for x in history if x.get('report_week')==day.isoformat()}
    rerun=[a for a in candidates if article_id(a) in current_ids]
    if rerun:
        fresh.extend(rerun)
        duplicates=[a for a in duplicates if article_id(a) not in current_ids]
    in_window=[]; outside=[]
    for a in fresh:
        d=parse_date(a.get('online_date'))
        if d and start<=d<=end and a.get('modules'): in_window.append(a)
        else: outside.append(a)
    metrics_lookup(in_window)
    out=REPORTS/day.isoformat(); out.mkdir(parents=True,exist_ok=True)
    audit={'run_date':day.isoformat(),'coverage_start':start.isoformat(),'coverage_end':end.isoformat(),'mode':'sample' if args.sample else 'online','candidates':len(candidates),'duplicates':len(duplicates),'out_of_window':len(outside),'included':len(in_window),'network_failures':failures,'limitations':['CAS 2025 分区与 JCR IF 未导入授权数据时均标注未核验。','在线模式只从 PubMed 获取候选；Europe PMC/Crossref/OpenAlex 扩展接口已预留为下一版本。']}
    (out/'audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/f'Weekly_Research_Report_{day:%Y%m%d}.html').write_text(make_html(in_window,topics,start,end,day.isoformat(),audit),encoding='utf-8')
    make_pdf(in_window,topics,start,end,day.isoformat(),out/f'Weekly_Research_Report_{day:%Y%m%d}.pdf')
    with (out/'included_records.csv').open('w',newline='',encoding='utf-8-sig') as f:
        fields=['pmid','doi','title','journal','online_date','type','modules','cas_2025_major','cas_2025_minor','impact_factor','evidence']; w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); [w.writerow({**a,'modules':';'.join(a['modules'])}) for a in in_window]
    if in_window and not rerun:
        with (DATA/'history.jsonl').open('a',encoding='utf-8') as f:
            for a in in_window: f.write(json.dumps({**a,'first_found':day.isoformat(),'report_week':day.isoformat()},ensure_ascii=False)+'\n')
    status='disabled'
    if not args.no_email: status=email_preview(out,day.strftime('%Y%m%d'),in_window,start,end,args.send_email,args.confirm_send)
    log={'timestamp':dt.datetime.now(dt.timezone.utc).isoformat(),'report':str(out),'email_status':status,**audit}
    (LOGS/f'run_{day.isoformat()}.json').write_text(json.dumps(log,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'report_dir':str(out),'included':len(in_window),'email':status,'failures':len(failures)},ensure_ascii=False))
if __name__=='__main__': main()
