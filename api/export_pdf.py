"""Premium PDF renderer for the MedicalExportModel.

Pipeline: model → ReportLab body (cover, executive summary, TOC with page numbers, sections, vector charts) →
PyMuPDF appendix (original documents appended page by page without rasterizing PDFs; images embedded at original
resolution) → GOTO links from every "Source Rnn" reference to its appendix page, back-links from each appendix page →
bookmarks → "Page X of Y" footer on every page. A4 throughout; output is deterministic for identical input.
"""
import io
import csv
import os
import re
import zipfile
from datetime import date, datetime
import fitz
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether, CondPageBreak, Flowable, NextPageTemplate
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing, Line, Rect, String, Circle, PolyLine, Polygon
from reportlab.pdfgen import canvas as rl_canvas
from .export_model import build_model, export_text, estimate_pages, fmt_d
from .storage import read_document
from .crypto import decrypt

# ------------------------------------------------------------------ palette (warm off-white, deep muted teal, charcoal)
PAPER=colors.HexColor('#FBF8F2');PAPER2=colors.HexColor('#F3EEE4');INK=colors.HexColor('#23303A');MUTED=colors.HexColor('#66727C')
TEAL=colors.HexColor('#1F5F6B');TEAL_SOFT=colors.HexColor('#DCEAEA');TEAL_DEEP=colors.HexColor('#174852');LINE=colors.HexColor('#DAD3C6');NAVY=colors.HexColor('#2B3A4B')
AMBER=colors.HexColor('#A66A1C');AMBER_SOFT=colors.HexColor('#F6E7CF');RED=colors.HexColor('#9A3B36');RED_SOFT=colors.HexColor('#F3DCD8');GREEN=colors.HexColor('#2E6B4F');GREEN_SOFT=colors.HexColor('#DCEBE1');SLATE=colors.HexColor('#5B6670');SLATE_SOFT=colors.HexColor('#E6E7E9')
STATUS_COLOURS={'PRESCRIBED':(SLATE,SLATE_SOFT),'SCHEDULED':(AMBER,AMBER_SOFT),'CONFIRMED GIVEN':(TEAL_DEEP,TEAL_SOFT),'UNKNOWN':(MUTED,PAPER2),'STOPPED':(SLATE,SLATE_SOFT),'NOT RECEIVED':(SLATE,SLATE_SOFT)}
KIND_COLOURS={'birth':NAVY,'admission':RED,'discharge':RED,'episode':AMBER,'fever':AMBER,'culture':TEAL_DEEP,'lab':TEAL,'antibiotic':NAVY,'medicine':SLATE,'vaccine':GREEN,'visit':SLATE,'growth':GREEN}
FONT_DIR=os.path.join(os.path.dirname(__file__),'assets','fonts')
def _fonts():
    if 'BCSans' in pdfmetrics.getRegisteredFontNames():return
    try:
        pdfmetrics.registerFont(TTFont('BCSans',os.path.join(FONT_DIR,'DejaVuSans.ttf')));pdfmetrics.registerFont(TTFont('BCSans-Bold',os.path.join(FONT_DIR,'DejaVuSans-Bold.ttf')));pdfmetrics.registerFont(TTFont('BCSans-Italic',os.path.join(FONT_DIR,'DejaVuSans-Oblique.ttf')))
        pdfmetrics.registerFont(TTFont('BCSerif',os.path.join(FONT_DIR,'DejaVuSerif.ttf')));pdfmetrics.registerFont(TTFont('BCSerif-Bold',os.path.join(FONT_DIR,'DejaVuSerif-Bold.ttf')))
        from reportlab.pdfbase.pdfmetrics import registerFontFamily
        registerFontFamily('BCSans',normal='BCSans',bold='BCSans-Bold',italic='BCSans-Italic',boldItalic='BCSans-Bold')
        registerFontFamily('BCSerif',normal='BCSerif',bold='BCSerif-Bold',italic='BCSerif',boldItalic='BCSerif-Bold')
    except Exception:  # fonts missing: fall back to the base-14 set (Latin-1 only)
        for a,b in (('BCSans','Helvetica'),('BCSans-Bold','Helvetica-Bold'),('BCSans-Italic','Helvetica-Oblique'),('BCSerif','Times-Roman'),('BCSerif-Bold','Times-Bold')):
            pdfmetrics.registerFont(pdfmetrics.Font(a,b,'WinAnsiEncoding'))
_fonts()
W,H=A4;ML=18*mm;MR=18*mm;MT=22*mm;MB=20*mm;CW=W-ML-MR
def S(name,**kw):
    base=dict(fontName='BCSans',fontSize=9.2,leading=12.6,textColor=INK)
    base.update(kw);return ParagraphStyle(name,**base)
ST={'body':S('body'),'small':S('small',fontSize=8,leading=10.5,textColor=MUTED),'tiny':S('tiny',fontSize=7,leading=9,textColor=MUTED),
 'h1':S('h1',fontName='BCSerif-Bold',fontSize=20,leading=25,textColor=TEAL_DEEP,spaceBefore=4,spaceAfter=8),
 'h2':S('h2',fontName='BCSans-Bold',fontSize=12.5,leading=16,textColor=TEAL_DEEP,spaceBefore=12,spaceAfter=5),
 'h3':S('h3',fontName='BCSans-Bold',fontSize=10,leading=13,textColor=NAVY,spaceBefore=8,spaceAfter=3),
 'cell':S('cell',fontSize=8,leading=10.2),'cellb':S('cellb',fontName='BCSans-Bold',fontSize=8,leading=10.2),'cellm':S('cellm',fontSize=7.6,leading=9.6,textColor=MUTED),
 'th':S('th',fontName='BCSans-Bold',fontSize=7.4,leading=9,textColor=colors.white),
 'cover_title':S('cover_title',fontName='BCSerif-Bold',fontSize=30,leading=36,textColor=TEAL_DEEP),'cover_sub':S('cover_sub',fontName='BCSerif',fontSize=15,leading=20,textColor=NAVY),
 'kpi_v':S('kpi_v',fontName='BCSerif-Bold',fontSize=19,leading=22,textColor=TEAL_DEEP),'kpi_l':S('kpi_l',fontSize=7.4,leading=9.5,textColor=MUTED),
 'note':S('note',fontSize=8.2,leading=11,textColor=NAVY,backColor=TEAL_SOFT,borderPadding=(5,7,5,7),spaceBefore=4,spaceAfter=8),
 'caution':S('caution',fontSize=8.2,leading=11,textColor=INK,backColor=AMBER_SOFT,borderPadding=(5,7,5,7),spaceBefore=4,spaceAfter=8),
 'toc1':S('toc1',fontSize=10,leading=15,fontName='BCSans-Bold',textColor=NAVY),'toc2':S('toc2',fontSize=8.6,leading=12.5,leftIndent=14,textColor=INK),'toc3':S('toc3',fontSize=7.8,leading=11,leftIndent=28,textColor=MUTED),
 'right':S('right',alignment=TA_RIGHT),'center':S('center',alignment=TA_CENTER)}
def esc(t):return (str(t) if t is not None else '').replace('&','&amp;').replace('<','&lt;').replace('>','&gt;')
def P(t,style='body',**kw):return Paragraph(t,ST[style] if isinstance(style,str) else style,**kw)
def link(ref,text=None,page_label=True):
    """Clickable source reference. href uses a private scheme that is rewritten to a GOTO link after the appendix merge."""
    if not ref:return '<font color="#66727C">no source</font>'
    label=esc(text or ref['label']);href=f"bcsrc:{ref['doc_id']}:{ref.get('page') or 1}"
    return f'<a href="{href}" color="#1F5F6B"><u>{label}</u></a>'
def chip(status):
    key=next((k for k in STATUS_COLOURS if status.startswith(k)),'UNKNOWN');fg,bg=STATUS_COLOURS[key]
    return Paragraph(f'<font color="{fg.hexval().replace("0x","#")}"><b>{esc(status)}</b></font>',ParagraphStyle('chip',parent=ST['cell'],fontSize=7,leading=9,backColor=bg,borderPadding=(1.5,3,1.5,3)))
def table(header,rows,widths,zebra=True,font=7.8,header_bg=TEAL,align=None,extra=None):
    data=[[P(f'{esc(h)}','th') for h in header]]+rows
    t=Table(data,colWidths=widths,repeatRows=1,hAlign='LEFT')
    style=[('BACKGROUND',(0,0),(-1,0),header_bg),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,0),0.6,header_bg),('LINEBELOW',(0,1),(-1,-1),0.3,LINE),('TOPPADDING',(0,0),(-1,-1),3.2),('BOTTOMPADDING',(0,0),(-1,-1),3.2),('LEFTPADDING',(0,0),(-1,-1),4),('RIGHTPADDING',(0,0),(-1,-1),4)]
    if zebra:style+=[('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,PAPER2])]
    if extra:style+=extra
    t.setStyle(TableStyle(style));return t
def cell(t,style='cell'):return P(esc(t) if not isinstance(t,str) or '<' not in t else t,style)
def kv_card(title,rows,width):
    body=[[P(f'<b>{esc(k)}</b>','cellm'),P(v if isinstance(v,str) else esc(v),'cell')] for k,v in rows]
    t=Table([[P(esc(title),'h3')]]+body,colWidths=[width*0.36,width*0.64],hAlign='LEFT')
    t.setStyle(TableStyle([('SPAN',(0,0),(1,0)),('BACKGROUND',(0,0),(-1,-1),colors.white),('BOX',(0,0),(-1,-1),0.5,LINE),('LINEBELOW',(0,0),(-1,0),0.8,TEAL_SOFT),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),6),('RIGHTPADDING',(0,0),(-1,-1),6),('TOPPADDING',(0,0),(-1,-1),2.5),('BOTTOMPADDING',(0,0),(-1,-1),2.5)]))
    return t
def kpis(items):
    cells=[[P(esc(v),'kpi_v') for v,_ in items],[P(esc(l),'kpi_l') for _,l in items]]
    t=Table(cells,colWidths=[CW/len(items)]*len(items),hAlign='LEFT')
    t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.white),('BOX',(0,0),(-1,-1),0.5,LINE),('LINEAFTER',(0,0),(-2,-1),0.5,LINE),('TOPPADDING',(0,0),(-1,0),8),('BOTTOMPADDING',(0,1),(-1,1),8),('LEFTPADDING',(0,0),(-1,-1),8)]))
    return t

class Anchor(Flowable):
    """Zero-height flowable that records the page number of a section and sets a named destination."""
    def __init__(self,key,title,level=0):super().__init__();self.key,self.title,self.level=key,title,level;self.width=0;self.height=0
    def draw(self):
        c=self.canv;pages=getattr(c,'_bc_pages',None)
        if pages is not None:pages[self.key]=c.getPageNumber()
        c.bookmarkPage(self.key)

class Chart(Flowable):
    def __init__(self,drawing):super().__init__();self.d=drawing;self.width=drawing.width;self.height=drawing.height
    def draw(self):self.d.drawOn(self.canv,0,0)

def ref_band(reference):
    m=re.findall(r'-?\d+(?:\.\d+)?',reference or '')
    if len(m)>=2:return float(m[0]),float(m[1])
    return None
def trend_chart(trend,width=CW/2-6,height=78):
    pts=trend['points'];d=Drawing(width,height);pad_l,pad_r,pad_t,pad_b=34,8,14,18
    xs=[datetime.fromisoformat(p['date']).toordinal() for p in pts];ys=[p['num'] for p in pts]
    bands=[ref_band(p['reference']) for p in pts];lo=min(ys+[b[0] for b in bands if b]);hi=max(ys+[b[1] for b in bands if b])
    if hi==lo:hi=lo+1
    span=(hi-lo)*0.15;lo-=span;hi+=span;x0,x1=min(xs),max(xs)
    if x1==x0:x1=x0+1
    X=lambda v:pad_l+(v-x0)/(x1-x0)*(width-pad_l-pad_r);Y=lambda v:pad_b+(v-lo)/(hi-lo)*(height-pad_t-pad_b)
    d.add(Rect(0,0,width,height,fillColor=colors.white,strokeColor=LINE,strokeWidth=0.5))
    d.add(String(6,height-11,f"{trend['test']}"+(f"  ({trend['unit']})" if trend['unit'] else ''),fontName='BCSans-Bold',fontSize=7.2,fillColor=NAVY))
    for i,(x,b) in enumerate(zip(xs,bands)):  # each report's own reference interval, drawn under its own point
        if b:
            w=max(6,(width-pad_l-pad_r)/max(1,len(set(xs)))*0.6)
            d.add(Rect(X(x)-w/2,Y(max(b[0],lo)),w,max(0.5,Y(min(b[1],hi))-Y(max(b[0],lo))),fillColor=TEAL_SOFT,strokeColor=None))
    for v in (lo+span,hi-span):d.add(String(pad_l-4,Y(v)-2.5,f"{v:g}",fontName='BCSans',fontSize=6,fillColor=MUTED,textAnchor='end'))
    d.add(Line(pad_l,pad_b,width-pad_r,pad_b,strokeColor=LINE,strokeWidth=0.5));d.add(Line(pad_l,pad_b,pad_l,height-pad_t,strokeColor=LINE,strokeWidth=0.5))
    d.add(PolyLine([c for x,y in zip(xs,ys) for c in (X(x),Y(y))],strokeColor=TEAL,strokeWidth=1.1))
    for x,y,p in zip(xs,ys,pts):d.add(Circle(X(x),Y(y),2,fillColor=TEAL_DEEP,strokeColor=colors.white,strokeWidth=0.6))
    seen=set()
    for x,p in zip(xs,pts):
        if x in seen:continue
        seen.add(x);d.add(String(X(x),4,datetime.fromisoformat(p['date']).strftime('%d %b %y'),fontName='BCSans',fontSize=5.6,fillColor=MUTED,textAnchor='middle'))
    return d
def course_gantt(rows,cultures,width=CW):
    dated=[r for r in rows if r['start']]
    if not dated:return None
    rowh=13;pad_l=150;pad_t=22;pad_b=16;height=pad_t+pad_b+rowh*len(dated)
    d=Drawing(width,height);d.add(Rect(0,0,width,height,fillColor=colors.white,strokeColor=LINE,strokeWidth=0.5))
    ds=[date.fromisoformat(r['start']) for r in dated]+[date.fromisoformat(r['end'] or r['start']) for r in dated]+[date.fromisoformat(c['date']) for c in cultures]
    x0,x1=min(ds).toordinal()-3,max(ds).toordinal()+3;X=lambda o:pad_l+(o-x0)/(x1-x0)*(width-pad_l-10)
    months=[];cur=date.fromordinal(x0).replace(day=1)
    while cur.toordinal()<=x1:
        months.append(cur);cur=(cur.replace(day=28)+__import__('datetime').timedelta(days=4)).replace(day=1)
    per=(width-pad_l-10)/max(1,len(months));step=max(1,-(-36//max(1,int(per))))  # label only every n-th month so labels never overlap
    for j,mth in enumerate(months):
        if mth.toordinal()>=x0:
            d.add(Line(X(mth.toordinal()),pad_b,X(mth.toordinal()),height-pad_t+4,strokeColor=LINE,strokeWidth=0.4))
            if j%step==0:d.add(String(X(mth.toordinal())+2,height-pad_t+7,mth.strftime('%b %Y'),fontName='BCSans',fontSize=6,fillColor=MUTED))
    for i,r in enumerate(dated):
        y=height-pad_t-rowh*(i+1)+3;a=date.fromisoformat(r['start']).toordinal();b=date.fromisoformat(r['end'] or r['start']).toordinal()+1
        fg,bg=STATUS_COLOURS[next((k for k in STATUS_COLOURS if r['status'].startswith(k)),'UNKNOWN')]
        solid=r['status_key']=='confirmed_administered'
        d.add(Rect(X(a),y,max(2.5,X(b)-X(a)),rowh-5,fillColor=(fg if solid else bg),strokeColor=fg,strokeWidth=0.7,strokeDashArray=(None if solid else [2,1.5])))
        d.add(String(6,y+2,f"{r['drug'][:26]} · {r['route']} · {r['status'].split(' (')[0].lower()}",fontName='BCSans',fontSize=6.3,fillColor=INK))
    for j,c in enumerate(cultures):
        xo=X(date.fromisoformat(c['date']).toordinal());d.add(Line(xo,pad_b,xo,height-pad_t+4,strokeColor=RED,strokeWidth=0.8,strokeDashArray=[1.5,1.5]));d.add(String(xo+2,pad_b-9+(7 if j%2 else 0),'culture '+datetime.fromisoformat(c['date']).strftime('%d %b'),fontName='BCSans',fontSize=5.6,fillColor=RED))
    d.add(String(pad_l,2,'Solid bar = administration documented · dashed = prescribed or scheduled only, no administration record · red line = culture collected',fontName='BCSans',fontSize=5.6,fillColor=MUTED))
    return d
def _num(v):
    try:return float(re.findall(r'-?\d+(?:\.\d+)?',str(v))[0])
    except Exception:return None
def growth_chart(rows,key,label,width=CW/2-6,height=90):
    pts=[(date.fromisoformat(r['date']).toordinal(),_num(r[key])) for r in rows if _num(r.get(key)) is not None]
    if len(pts)<2:return None
    d=Drawing(width,height);pad_l,pad_r,pad_t,pad_b=30,8,14,18;xs=[p[0] for p in pts];ys=[p[1] for p in pts];lo,hi=min(ys),max(ys);lo-=(hi-lo)*0.1 or 1;hi+=(hi-lo)*0.1 or 1
    X=lambda v:pad_l+(v-xs[0])/max(1,xs[-1]-xs[0])*(width-pad_l-pad_r);Y=lambda v:pad_b+(v-lo)/(hi-lo)*(height-pad_t-pad_b)
    d.add(Rect(0,0,width,height,fillColor=colors.white,strokeColor=LINE,strokeWidth=0.5));d.add(String(6,height-11,label,fontName='BCSans-Bold',fontSize=7.2,fillColor=NAVY))
    d.add(Line(pad_l,pad_b,width-pad_r,pad_b,strokeColor=LINE,strokeWidth=0.5));d.add(Line(pad_l,pad_b,pad_l,height-pad_t,strokeColor=LINE,strokeWidth=0.5))
    d.add(PolyLine([c for x,y in pts for c in (X(x),Y(y))],strokeColor=GREEN,strokeWidth=1.1))
    for x,y in pts:d.add(Circle(X(x),Y(y),1.8,fillColor=GREEN,strokeColor=colors.white,strokeWidth=0.5))
    for v in (min(ys),max(ys)):d.add(String(pad_l-3,Y(v)-2.5,f"{v:g}",fontName='BCSans',fontSize=6,fillColor=MUTED,textAnchor='end'))
    for x,_ in (pts[0],pts[len(pts)//2],pts[-1]):d.add(String(X(x),4,date.fromordinal(x).strftime('%b %y'),fontName='BCSans',fontSize=5.6,fillColor=MUTED,textAnchor='middle'))
    return d
def care_chart(series,width=CW,height=95):
    if not series:return None
    d=Drawing(width,height);pad_l,pad_r,pad_t,pad_b=30,8,14,18;n=len(series);bw=(width-pad_l-pad_r)/n
    mx=max([s.get('fluids_ml') or 0 for s in series]+[1]);d.add(Rect(0,0,width,height,fillColor=colors.white,strokeColor=LINE,strokeWidth=0.5))
    d.add(String(6,height-11,'Fluids recorded per day (ml) · wet diapers · stool entries',fontName='BCSans-Bold',fontSize=7.2,fillColor=NAVY))
    for i,s in enumerate(series):
        x=pad_l+i*bw;v=s.get('fluids_ml') or 0;h=(v/mx)*(height-pad_t-pad_b)
        d.add(Rect(x+bw*0.15,pad_b,bw*0.7,h,fillColor=TEAL_SOFT,strokeColor=TEAL,strokeWidth=0.5))
        d.add(String(x+bw/2,pad_b+h+2,str(v) if v else '',fontName='BCSans',fontSize=5.6,fillColor=TEAL_DEEP,textAnchor='middle'))
        d.add(String(x+bw/2,4,date.fromisoformat(s['date']).strftime('%d %b'),fontName='BCSans',fontSize=5.6,fillColor=MUTED,textAnchor='middle'))
        d.add(String(x+bw/2,height-pad_t+3,f"W{s.get('wet_diapers',0)} S{s.get('stools',0)}",fontName='BCSans',fontSize=5.4,fillColor=MUTED,textAnchor='middle'))
    return d

# ------------------------------------------------------------------ page decorations
class _Canvas(rl_canvas.Canvas):
    def __init__(self,*a,**kw):
        kw.setdefault('invariant',1);super().__init__(*a,**kw);self._bc_pages=_Canvas.pages_sink
    pages_sink=None
def _decorate(model):
    head=f"HealthBook AI · {model['patient']['name']} · generated {model['meta']['generated_display']}"
    def on_page(c,doc):
        c.saveState();c.setFillColor(PAPER);c.rect(0,0,W,H,stroke=0,fill=1)
        if doc.page>1:
            c.setStrokeColor(LINE);c.setLineWidth(0.5);c.line(ML,H-MT+9*mm,W-MR,H-MT+9*mm)
            c.setFont('BCSans',7.2);c.setFillColor(MUTED);c.drawString(ML,H-MT+11*mm,head);c.drawRightString(W-MR,H-MT+11*mm,model['meta']['title'])
            c.setFont('BCSans',7);c.drawString(ML,MB-9*mm,'Family-maintained record · statuses are copied from source documents, never inferred')
        c.restoreState()
    return on_page

# ------------------------------------------------------------------ body builder
def _story(model,pages,appendix_pages):
    m=model;secs=set(m['meta']['sections']);p=m['patient'];st=[];add=st.append
    counts=m['counts'];src_total=appendix_pages.get('_total_pages')
    def ref_cell(ref):return P(link(ref),'cellm')
    def section(key,title,sub=None):
        add(CondPageBreak(60*mm));add(Anchor(key,title));add(P(esc(title),'h1'))
        if sub:add(P(sub,'small'));add(Spacer(1,4))
    # cover ---------------------------------------------------------------
    add(Spacer(1,38*mm));add(Anchor('cover','Cover'))
    add(Table([[P('HEALTHBOOK AI · MEDICAL RECORD',S('ov',fontSize=8.5,leading=11,textColor=TEAL,fontName='BCSans-Bold'))]],colWidths=[CW],style=[('LINEBELOW',(0,0),(-1,0),1.2,TEAL),('LEFTPADDING',(0,0),(-1,-1),0),('BOTTOMPADDING',(0,0),(-1,-1),6)]))
    add(Spacer(1,10));add(P(esc(m['meta']['title']),'cover_title'));add(Spacer(1,6));add(P(esc(p['name']),'cover_sub'));add(Spacer(1,16))
    dr=m['meta']['date_range'];rng=f"{fmt_d(date.fromisoformat(dr['from'])) if dr.get('from') else '—'} to {fmt_d(date.fromisoformat(dr['to'])) if dr.get('to') else '—'}"+(' (span of documented events)' if dr.get('derived') else ' (selected range)')
    rows=[('Date of birth',f"{fmt_d(date.fromisoformat(p['dob']))} · age {p['age']} at generation"),('Export type',esc(m['meta']['description'])),('Record range',rng),('Generated',m['meta']['generated_display']+f" ({p['tz']})"),('Contents','Summary · '+(' · '.join(s.replace('_',' ') for s in m['meta']['sections'] if s not in('summary','sources'))))]
    if 'sources' in secs:rows.append(('Appendix',f"{counts['sources']} original source document(s), {counts['source_pages']} page(s), appended unchanged"))
    t=Table([[P(f'<b>{k}</b>','cellm'),P(v,'body')] for k,v in rows],colWidths=[34*mm,CW-34*mm],hAlign='LEFT');t.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,-1),0.3,LINE),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),('LEFTPADDING',(0,0),(-1,-1),0)]));add(t)
    add(Spacer(1,18));add(kpis([(str(counts['sources']),'source documents'),(str(counts['hospitalizations']),'hospitalizations'),(str(counts['prescriptions']),'prescriptions'),(str(counts['antibiotic_courses']),'antibiotic courses'),(str(counts['lab_documents']),'laboratory reports'),(str(counts['vaccinations']),'vaccination entries')]))
    add(Spacer(1,22));add(P(esc(m['meta']['note'])+' Where a status reads PRESCRIBED, no administration record exists in this record; CONFIRMED GIVEN means an administration record or hospital statement exists. Culture susceptibility results describe laboratory testing of an isolate and are never medicines given.','small'))
    if m['meta'].get('reason'):add(Spacer(1,6));add(P('Reason for export: '+esc(m['meta']['reason']),'small'))
    add(PageBreak())
    # executive summary -----------------------------------------------------
    sm=m['summary'];section('summary','Executive clinical summary','Documented in available records. Each line names its source; no figure here is estimated.')
    half=CW/2-4;left=[];right=[]
    if sm['hospitalizations']:left.append(kv_card('Hospitalizations',[(h['dates'],esc(h['facility'])+(f"<br/><font color='#66727C'>{esc(h['diagnosis'][:160])}</font>" if h['diagnosis'] else '')+' '+link(h['ref'])) for h in sm['hospitalizations']],half))
    if sm['episodes']:left.append(kv_card('Illness episodes',[(e['dates'],esc(e['title'])+(f"<br/><font color='#66727C'>{esc(e['diagnosis'][:140])}</font>" if e['diagnosis'] else '')+' '+link(e['ref'])) for e in sm['episodes']],half))
    if sm['antibiotics']:
        a=sm['antibiotics'];left.append(kv_card('Antibiotic exposure (documented)',[('Distinct agents',f"{len(a['distinct'])}: "+esc(', '.join(a['distinct']))),('Courses',str(a['courses'])),('Prescribed only',f"{a['prescribed_only']} (no administration record)"),('Hospital statement only',f"{a['statement_only']} (given per discharge summary, no dated chart)"),('Dated administration days',str(a['confirmed_days'])),('Status unknown',str(a['unknown']))],half))
    if sm['cultures']:left.append(kv_card('Cultures',[(fmt_d(date.fromisoformat(c['date'])),esc(f"{c['test']} ({c['specimen'] or 'specimen not stated'}): {c['organism'] or '—'}")+' '+link(c['ref'])) for c in sm['cultures']],half))
    if sm['medicines']:right.append(kv_card('Medicines in the record (status as documented)',[(r['name'][:28],f"{esc(r['dose'])} · {esc(r['frequency'])}<br/><b>{esc(r['status'])}</b> · {esc(r['context'])} · {esc(r['dates'])}") for r in sm['medicines']],half))
    if sm['lab_trends']:right.append(kv_card('Key laboratory values (latest vs previous)',[(t['test'][:26],f"{esc(t['latest']['value'])} {esc(t['latest']['unit'] or '')} on {fmt_d(date.fromisoformat(t['latest']['date']))}<br/><font color='#66727C'>previous {esc(t['previous']['value'])} on {fmt_d(date.fromisoformat(t['previous']['date']))} · interval {esc(t['latest']['reference'] or 'not printed')}</font>") for t in sm['lab_trends']],half))
    if m['vaccinations']:
        v=sm['vaccination'];right.append(kv_card('Vaccination status',[('Given, verified',str(v['given_verified'])),('Given on card, pending verification',str(v['given_pending'])),('Upcoming / due',', '.join(f"{x['name']} {x['dose']}".strip() for x in v['upcoming']) or 'none listed'),('Due date passed, no given date',', '.join(f"{x['name']} {x['dose']}".strip() for x in v['overdue']) or 'none')],half))
    if sm['growth']['latest']:
        g=sm['growth']['latest'];right.append(kv_card('Growth (latest documented)',[(fmt_d(date.fromisoformat(g['date'])),' · '.join(x for x in (f"{g['weight']} kg" if g['weight'] else '',f"{g['length']} cm" if g['length'] else '',f"head {g['head']} cm" if g['head'] else '') if x)),('Measurements',str(sm['growth']['count']))],half))
    right.append(kv_card('Verification status',[('Items pending verification',str(sm['pending_count'])),('Meaning','Transcribed from documents and not yet confirmed by a caregiver. The original page is appended for every item.')],half))
    grid=[];L=left[:];R=right[:]
    while L or R:grid.append([L.pop(0) if L else '',R.pop(0) if R else ''])
    g=Table(grid,colWidths=[half,half],hAlign='LEFT',style=[('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8)]);add(g)
    if sm['recent']:
        add(P('Documented events in the last 90 days','h2'));add(table(['Date','Event','Detail','Source'],[[cell(fmt_d(date.fromisoformat(t['date']))),cell(t['title'],'cellb'),cell(t['detail'] or ''),ref_cell(t['ref'])] for t in sm['recent']],[22*mm,52*mm,CW-22*mm-52*mm-30*mm,30*mm]))
    add(PageBreak())
    # table of contents -----------------------------------------------------
    add(Anchor('toc','Contents'));add(P('Contents','h1'))
    toc_items=[('summary','Executive clinical summary',1)]
    if 'timeline' in secs and m['timeline']:toc_items.append(('timeline','Medical timeline',1))
    if 'hospitalizations' in secs and m['hospitalizations']:toc_items.append(('hosp','Hospitalizations',1));toc_items+=[(f"hosp{h['id']}",f"{fmt_d(date.fromisoformat(h['admitted']))} · {h['facility']}",2) for h in m['hospitalizations']]
    if 'illnesses' in secs and m['illnesses']:toc_items.append(('ill','Illness episodes',1))
    if 'medicines' in secs and m['medicines']:toc_items.append(('meds','Medications',1))
    if 'antibiotics' in secs and m['antibiotics']['rows']:toc_items.append(('abx','Antibiotic history',1))
    if 'labs' in secs and m['labs']:toc_items.append(('labs','Laboratory results',1));toc_items+=[(f"lab_{i}",g['category'],2) for i,g in enumerate(m['labs'])]
    if 'cultures' in secs and m['cultures']:toc_items.append(('micro','Microbiology and E. coli',1))
    if 'vaccinations' in secs and m['vaccinations']:toc_items.append(('vax','Vaccinations',1))
    if 'growth' in secs and m['growth']:toc_items.append(('growth','Growth',1))
    if 'daily_care' in secs and (m['daily_care'] or m.get('vitals')):toc_items.append(('care','Daily Health journal',1))
    if m['pending']:toc_items.append(('pending','Items pending verification',1))
    if 'sources' in secs and m['sources']:toc_items.append(('srcidx','Original source documents (index)',1));toc_items.append(('appendix','Appendix: original documents',1))
    rows=[]
    for key,title,lvl in toc_items:
        if key=='appendix':
            pg=(appendix_pages[m['sources'][0]['id']]-1) if m['sources'] and appendix_pages.get(m['sources'][0]['id']) else '';href=f'bcsrc:{m["sources"][0]["id"]}:0' if m['sources'] else '#srcidx'
        else:pg=pages.get(key,'');href=f'#{key}'
        rows.append([P(f'<a href="{href}" color="#23303A">{esc(title)}</a>','toc1' if lvl==1 else 'toc2'),P(str(pg),'toc1' if lvl==1 else 'toc2')])
    if 'sources' in secs:
        for s in m['sources']:
            pg=appendix_pages.get(s['id'],'');rows.append([P(f'<a href="bcsrc:{s["id"]}:1" color="#66727C">Appendix {s["appendix"]} · {esc(s["ref"])} · {esc(fmt_d(date.fromisoformat(s["record_date"])) if s["record_date"] else "undated")} · {esc(s["title"][:70])}</a>','toc3'),P(str(pg),'toc3')])
    t=Table(rows,colWidths=[CW-16*mm,16*mm],hAlign='LEFT');t.setStyle(TableStyle([('ALIGN',(1,0),(1,-1),'RIGHT'),('VALIGN',(0,0),(-1,-1),'TOP'),('LINEBELOW',(0,0),(-1,-1),0.25,LINE),('TOPPADDING',(0,0),(-1,-1),1.5),('BOTTOMPADDING',(0,0),(-1,-1),1.5),('LEFTPADDING',(0,0),(-1,-1),0)]));add(t)
    add(PageBreak())
    # timeline ----------------------------------------------------------------
    if 'timeline' in secs and m['timeline']:
        section('timeline','Medical timeline','Every documented event in date order. The source column opens the original page in the appendix.')
        rows=[]
        for t in m['timeline']:
            dot=Table([['']],colWidths=[3.2*mm],rowHeights=[3.2*mm],style=[('BACKGROUND',(0,0),(0,0),KIND_COLOURS.get(t['kind'],SLATE))])
            rows.append([cell(fmt_d(date.fromisoformat(t['date']))),dot,P(f"<b>{esc(t['title'])}</b>"+(f"<br/><font color='#66727C'>{esc(t['detail'])}</font>" if t['detail'] else ''),'cell'),ref_cell(t['ref'])])
        add(table(['Date','','Event','Source'],rows,[22*mm,6*mm,CW-22*mm-6*mm-30*mm,30*mm]))
        add(Spacer(1,4));add(P('Colour key: red hospital admission or discharge · amber illness or fever · teal laboratory and culture · navy antibiotic · grey other medicine · green vaccination or growth.','tiny'))
    # hospitalizations --------------------------------------------------------
    if 'hospitalizations' in secs and m['hospitalizations']:
        section('hosp','Hospitalizations')
        for h in m['hospitalizations']:
            add(Anchor(f"hosp{h['id']}",h['facility'],1));add(P(f"{esc(h['facility'])}",'h2'))
            add(kv_card('Stay',[('Admitted',fmt_d(date.fromisoformat(h['admitted']))),('Discharged',fmt_d(date.fromisoformat(h['discharged'])) if h['discharged'] else 'not documented'),('Length',f"{h['length_days']} day(s)" if h['length_days'] else '—'),('Treating doctor',h['doctor'] or '—'),('Diagnosis as written',h['diagnosis'] or '—'),('Presenting complaints',h['complaints'] or '—'),('Sources',(link(h['ref'])+(' · discharge summary '+link(h['discharge_ref']) if h['discharge_ref'] and h['discharge_ref']!=h['ref'] else '')))],CW))
            if h['course']:add(P('Hospital course (as documented)','h3'));add(P(esc(h['course']),'body'))
            if h['treatment']:add(P('Treatment given during hospitalization (as documented)','h3'));add(P(esc(h['treatment']),'body'))
            if h['inpatient_listed']:
                add(P('Medicines listed as given in hospital','h3'))
                add(table(['Medicine','Dose','Route','Frequency','Status','Source'],[[cell(r['name'],'cellb'),cell(r['dose']),cell(r['route']),cell(r['frequency']),chip(r['status']),ref_cell(r['ref'])] for r in h['inpatient_listed']],[46*mm,24*mm,18*mm,26*mm,CW-46*mm-24*mm-18*mm-26*mm-26*mm,26*mm]))
            if h['discharge_medicines']:
                add(P('Discharge prescription','h3'))
                add(table(['Medicine','Dose','Route','Frequency','Status','Source'],[[cell(r['name'],'cellb'),cell(r['dose']),cell(r['route']),cell(r['frequency']),chip(r['status']),ref_cell(r['ref'])] for r in h['discharge_medicines']],[46*mm,24*mm,18*mm,26*mm,CW-46*mm-24*mm-18*mm-26*mm-26*mm,26*mm]))
            if h['follow_up']:add(P('Follow-up instructions','h3'));add(P(esc(h['follow_up']),'body'))
            if h['labs_in_stay']:add(P(f"{len(h['labs_in_stay'])} laboratory value(s) are dated inside this stay; see Laboratory results.",'small'))
            add(Spacer(1,8))
    # illnesses ---------------------------------------------------------------
    if 'illnesses' in secs and m['illnesses']:
        section('ill','Illness episodes','Episodes as documented; antibiotic status per course is copied from the record.')
        abx_by_id={r['id']:r for r in m['antibiotics']['rows']}
        for e in m['illnesses']:
            rows=[('Dates',fmt_d(date.fromisoformat(e['start']))+(' – '+fmt_d(date.fromisoformat(e['end'])) if e['end'] else ' – end not documented')),('Symptoms',e['symptoms'] or '—'),('Documented diagnosis',e['diagnosis'] or '—')]
            if e['fevers']:rows.append(('Fever',' · '.join(f"{esc(f['title'])}{(' '+f['temp']) if f['temp'] else ''}{' (parent-reported)' if f['origin']=='parent_report' else ''} {link(f['ref'])}" for f in e['fevers'])))
            if e['courses']:rows.append(('Antibiotics',' · '.join(f"{esc(abx_by_id[c]['drug'])} ({abx_by_id[c]['route']}) <b>{esc(abx_by_id[c]['status'].split(' (')[0])}</b>" for c in e['courses'] if c in abx_by_id)))
            rows.append(('Hospitalized','yes' if e['hospitalized'] else 'no admission documented'));rows.append(('Laboratory values in window',str(len(e['labs']))))
            if e['notes']:rows.append(('Notes',e['notes']))
            rows.append(('Sources',' '.join([link(e['ref'])]+[link(r) for r in e['doc_refs'][:6]])))
            add(KeepTogether([kv_card(e['title'],rows,CW),Spacer(1,6)]))
    # medications -------------------------------------------------------------
    if 'medicines' in secs and m['medicines']:
        section('meds','Medications','Status legend: PRESCRIBED = appears on a prescription, no administration record · SCHEDULED = reminder set by the family · CONFIRMED GIVEN = administration record exists · UNKNOWN = status cannot be determined from the record. A prescription is never shown as given.')
        add(table(['Medicine','Dose','Route','Frequency','Context','Dates','Status','Source'],[[P(f"<b>{esc(r['name'])}</b>"+(f"<br/><font color='#66727C'>{esc(r['generic'])}</font>" if r['generic'] and r['generic'].casefold()!=r['name'].casefold() else ''),'cell'),cell(r['dose']),cell(r['route']),cell(r['frequency']),cell(r['context'],'cellm'),cell(r['dates'],'cellm'),chip(r['status']),ref_cell(r['ref'])] for r in m['medicines']],[33*mm,18*mm,13*mm,20*mm,22*mm,22*mm,CW-33*mm-18*mm-13*mm-20*mm-22*mm-22*mm-22*mm,22*mm]))
        given=[(r,g) for r in m['medicines'] for g in r['given_events']]
        if given:add(P('Administration records','h3'));add(table(['Medicine','Given at','Recorded by','Source'],[[cell(r['name']),cell(datetime.fromisoformat(g['at']).strftime('%d %b %Y %H:%M')),cell(g['source']),ref_cell(g['ref'])] for r,g in given],[50*mm,40*mm,40*mm,CW-130*mm]))
    # antibiotics -------------------------------------------------------------
    if 'antibiotics' in secs and m['antibiotics']['rows']:
        a=m['antibiotics'];section('abx','Antibiotic history','Each course keeps its documented status. Metrics count only what the record supports.')
        if a['metrics']:add(kpis([(str(len(a['metrics']['distinct'])),'distinct agents'),(str(a['metrics']['courses']),'courses'),(str(a['metrics']['hospital_courses']),'in hospital'),(str(a['metrics']['prescribed_only']),'prescribed only'),(str(a['metrics']['statement_only']),'statement only'),(str(a['metrics']['confirmed_days']),'dated exposure days')]));add(Spacer(1,8))
        g=course_gantt(a['rows'],m['cultures'])
        if g:add(Chart(g));add(Spacer(1,8))
        add(table(['Antibiotic','Route · setting','Dates','Dose','Reason / episode','Duration','Status','Source'],[[P(f"<b>{esc(r['drug'])}</b>"+(f"<br/><font color='#66727C'>{esc(r['brand'])}</font>" if r['brand'] else '')+(f"<br/><font color='#A66A1C'>{esc(r['class'])}</font>" if r['class']=='unclassified' else ''),'cell'),cell(f"{r['route']} · {r['setting']}"),cell((fmt_d(date.fromisoformat(r['start'])) if r['start'] else '—')+(' – '+fmt_d(date.fromisoformat(r['end'])) if r['end'] and r['end']!=r['start'] else '')+('' if r['dates_known'] else ' (admission window)'),'cellm'),cell(r['dose']),cell(r['reason'],'cellm'),cell(r['prescribed_duration'],'cellm'),chip(r['status']),ref_cell(r['ref'])] for r in a['rows']],[30*mm,20*mm,26*mm,17*mm,26*mm,14*mm,CW-30*mm-20*mm-26*mm-17*mm-26*mm-14*mm-21*mm,21*mm]))
        adm=[(r,x) for r in a['rows'] for x in r['administrations']]
        if adm:add(P('Dated administrations','h3'));add(table(['Antibiotic','Date','Time','Recorded from','Source'],[[cell(r['drug']),cell(fmt_d(date.fromisoformat(x['on']))),cell(x['time'] or '—'),cell(x['source']),ref_cell(x['ref'])] for r,x in adm],[45*mm,30*mm,20*mm,40*mm,CW-135*mm]))
        else:add(P('No dated administration chart is present in the record. Hospital courses marked CONFIRMED GIVEN rest on the discharge summary statement; prescribed courses have no administration record at all.','caution'))
    # labs --------------------------------------------------------------------
    if 'labs' in secs and m['labs']:
        section('labs','Laboratory results','Values exactly as transcribed, with the reference interval printed on each report. Results from different reports on the same day are shown separately.')
        if m['same_day_conflicts']:
            names=sorted({c['test'] for c in m['same_day_conflicts']});add(P(f"Two reports dated {fmt_d(date.fromisoformat(m['same_day_conflicts'][0]['date']))} give different values for {len(names)} test(s) ({esc(', '.join(names[:6]))}{'…' if len(names)>6 else ''}). Both are shown with their own source; neither is preferred.",'caution'))
        if m['trends']:
            add(P('Trends','h3'));charts=[Chart(trend_chart(t)) for t in m['trends'][:12]];grid=[charts[i:i+2] for i in range(0,len(charts),2)]
            for row in grid:
                if len(row)==1:row.append('')
            add(Table(grid,colWidths=[CW/2,CW/2],hAlign='LEFT',style=[('LEFTPADDING',(0,0),(-1,-1),0),('RIGHTPADDING',(0,0),(-1,-1),6),('BOTTOMPADDING',(0,0),(-1,-1),6),('VALIGN',(0,0),(-1,-1),'TOP')]))
            add(P('Shaded band = reference interval printed on that report, drawn under its own point. Intervals differ between laboratories and with age.','tiny'))
        for i,g in enumerate(m['labs']):
            add(CondPageBreak(40*mm));add(Anchor(f'lab_{i}',g['category'],1));add(P(esc(g['category']).upper(),'h2'))
            rows=[]
            for r in g['rows']:
                band=ref_band(r['reference']);num=None
                try:num=float(re.findall(r'-?\d+(?:\.\d+)?',r['value'])[0]) if r['value'] else None
                except Exception:num=None
                flag='';
                if band and num is not None and (num<band[0] or num>band[1]):flag='<font color="#A66A1C">outside printed interval</font>'
                rows.append([cell(fmt_d(date.fromisoformat(r['date']))),P(f"<b>{esc(r['test'])}</b>"+(f"<br/><font color='#66727C'>{esc(r['original_name'])}</font>" if r['original_name'] and r['original_name'].casefold()!=r['test'].casefold() else ''),'cell'),cell(r['value'],'cellb'),cell(r['unit'] or ''),cell(r['reference'] or 'not printed','cellm'),P(flag+('' if r['verified'] else (' · ' if flag else '')+'<font color="#66727C">unverified</font>'),'cellm'),ref_cell(r['ref'])])
            add(table(['Date','Test','Result','Unit','Reference interval (this report)','Note','Source'],rows,[20*mm,42*mm,20*mm,17*mm,30*mm,CW-20*mm-42*mm-20*mm-17*mm-30*mm-23*mm,23*mm]))
    # microbiology --------------------------------------------------------------
    if 'cultures' in secs and m['cultures']:
        section('micro','Microbiology and E. coli','Culture findings as reported. Susceptibility describes laboratory testing of the isolate and is not a record of medicines given.')
        for c in m['cultures']:
            add(kv_card(f"{c['test']} · {fmt_d(date.fromisoformat(c['date']))}",[('Specimen',c['specimen'] or 'not stated'),('Organism',c['organism'] or '—'),('Findings',c['findings'] or c['value'] or '—'),('Verification','verified' if c['verified'] else 'pending caregiver verification'),('Source',link(c['ref']))],CW));add(Spacer(1,5))
        mx=m['susceptibility_matrix']
        if mx:
            add(P('Susceptibility comparison across cultures','h3'))
            hdr=['Antibiotic tested']+[f"{fmt_d(date.fromisoformat(c['date']))}\n{c['specimen'] or ''}" for c in mx['columns']]+['Named in a prescription or course']
            def mk(v):
                col={'S':GREEN,'R':RED,'I':AMBER}.get(v,MUTED);txt={'S':'S · susceptible','R':'R · resistant','I':'I · intermediate'}.get(v,'not tested')
                return Paragraph(f'<font color="{col.hexval().replace("0x","#")}"><b>{txt}</b></font>',ST['cell'])
            rows=[[cell(r['drug'],'cellb')]+[mk(v) for v in r['marks']]+[cell('yes' if r['prescribed_or_given'] else '—','cellm')] for r in mx['rows']]
            n=len(mx['columns']);cw=[44*mm]+[(CW-44*mm-34*mm)/n]*n+[34*mm];add(table(hdr,rows,cw))
            add(P('"Named in a prescription or course" means the same agent appears in the medication record; it does not mean it was given. Administration status is shown in Antibiotic history.','tiny'))
    # vaccinations ---------------------------------------------------------------
    if 'vaccinations' in secs and m['vaccinations']:
        section('vax','Vaccinations','A due date is a schedule entry; only a given date with a source is a documented dose. Card entries not yet verified by a caregiver are marked.')
        add(table(['Vaccine','Dose','Due','Given','Status','Recorded from','Source'],[[cell(v['name'],'cellb'),cell(v['dose'] or ''),cell(fmt_d(date.fromisoformat(v['due'])) if v['due'] else '—'),cell(fmt_d(date.fromisoformat(v['given'])) if v['given'] else 'not recorded','cellb' if v['given'] else 'cellm'),P((esc(v['status']))+('' if v['confirmed'] else ' <font color="#A66A1C">· unverified</font>'),'cellm'),cell(v['source_type'],'cellm'),ref_cell(v['ref'])] for v in m['vaccinations']],[36*mm,18*mm,22*mm,22*mm,CW-36*mm-18*mm-22*mm-22*mm-24*mm-24*mm,24*mm,24*mm]))
    # growth -----------------------------------------------------------------------
    if 'growth' in secs and m['growth']:
        section('growth','Growth','Measurements as documented; no percentile or interpretation is added.')
        ch=[c for c in (growth_chart(m['growth'],'weight','Weight (kg)'),growth_chart(m['growth'],'length','Length (cm)')) if c]
        if ch:add(Table([[Chart(c) for c in ch]+(['']*(2-len(ch)))],colWidths=[CW/2,CW/2],hAlign='LEFT',style=[('LEFTPADDING',(0,0),(-1,-1),0)]));add(Spacer(1,6))
        add(table(['Date','Age','Weight (kg)','Length (cm)','Head (cm)','Milestone','Source'],[[cell(fmt_d(date.fromisoformat(g['date']))),cell(g['age']),cell(g['weight'] or '—'),cell(g['length'] or '—'),cell(g['head'] or '—'),cell(g['milestone'] or '','cellm'),ref_cell(g['ref'])] for g in m['growth']],[22*mm,26*mm,20*mm,20*mm,20*mm,CW-22*mm-26*mm-60*mm-24*mm,24*mm]))
    # daily care ---------------------------------------------------------------------
    if 'daily_care' in secs and (m['daily_care'] or m.get('vitals')):
        c=m['daily_care'] or {'start':m['vitals']['start'],'end':m['vitals']['end'],'totals':{},'series':[],'statements':[],'symptom_temps':[],'sections':[],'photo_ids':[],'warnings':[]}
        section('care','Daily Health journal',f"Journal {fmt_d(date.fromisoformat(c['start']))} – {fmt_d(date.fromisoformat(c['end']))}, recorded by the family. Totals are what was recorded; only an explicit GIVEN entry counts as a medicine given; vitals are shown exactly as entered.")
        t=c['totals'];add(kpis([(str(t.get('fluids_ml',0)),'ml fluids recorded'),(str(t.get('wet_diapers',0)),'wet diapers'),(str(t.get('stools',0)),'stool entries'),(str(t.get('medicines_given',0)),'medicines given'),(str(t.get('sleep_minutes',0)),'min sleep recorded'),(str(t.get('days_with_entries',len(c['series'])) if isinstance(t,dict) else 0),'days with entries')]));add(Spacer(1,6))
        ch=care_chart(c['series'])
        if ch:add(Chart(ch));add(Spacer(1,6))
        if c['statements']:add(P('Observations (recorded, not interpreted)','h3'));[add(P('· '+esc(s),'body')) for s in c['statements']]
        if c['symptom_temps']:add(P('Temperatures recorded','h3'));add(table(['Date','Time','Temperature'],[[cell(fmt_d(date.fromisoformat(s['date']))),cell(datetime.fromisoformat(s['at']).strftime('%H:%M')),cell(f"{s['temperature']} °{s['unit'] or 'C'}")] for s in c['symptom_temps']],[30*mm,25*mm,CW-55*mm]))
        for day in c['sections']:
            add(P(fmt_d(date.fromisoformat(day['date'])),'h3'));add(table(['Time · entry'],[[cell(i)] for i in day['items']],[CW]))
        if c['photo_ids']:add(P(f"{len(c['photo_ids'])} stool photograph(s) were explicitly included and follow at the end of the appendix.",'note'))
        for w in c.get('warnings',[])[:1]:add(P(esc(w),'tiny'))
        v=m.get('vitals')
        if v:
            add(P('Vitals (as recorded)','h3'))
            for st in v['statements']:add(P('· '+esc(st),'body'))
            add(table(['When','Blood pressure','Heart rate','SpO2','Glucose','Weight','Temp.','Meal / note'],[[cell(datetime.fromisoformat(r['at']).strftime('%d %b %Y %H:%M')),cell(f"{r['systolic']}/{r['diastolic']}" if r['systolic'] else '—','cellb'),cell(r['heart_rate'] or '—'),cell(f"{r['spo2']}%" if r['spo2'] else '—'),cell((f"{r['glucose_mg_dl']:g}"+(f" ({r['glucose_context'].replace('_',' ')})" if r['glucose_context'] else '')) if r['glucose_mg_dl'] is not None else '—'),cell(f"{r['weight_kg']:g} kg" if r['weight_kg'] is not None else '—'),cell(f"{r['temperature_c']:g} °C" if r['temperature_c'] is not None else '—'),cell(' · '.join(x for x in (r['meal'],r['note']) if x),'cellm')] for r in v['rows']],[30*mm,22*mm,16*mm,14*mm,26*mm,18*mm,16*mm,CW-142*mm]))
            add(P('No reading is interpreted; ranges and targets are a matter for the treating clinician.','tiny'))
    # pending ------------------------------------------------------------------------
    if m['pending']:
        section('pending','Items pending verification','Transcribed entries not yet confirmed by a caregiver. Each is included above with its source page so a clinician can check the original.')
        add(table(['Item'],[[cell(x)] for x in m['pending']],[CW]))
    # sources index ---------------------------------------------------------------------
    if 'sources' in secs and m['sources']:
        section('srcidx','Original source documents (index)','Every document selected for this export, appended unchanged in the appendix in the order below.')
        add(table(['Appx','Ref','Date','Category','Title','Pages','Appendix page'],[[cell(s['appendix'],'cellb'),cell(s['ref']),cell(fmt_d(date.fromisoformat(s['record_date'])) if s['record_date'] else 'undated'),cell(s['category'].replace('_',' ')),P(f'<a href="bcsrc:{s["id"]}:1" color="#1F5F6B"><u>{esc(s["title"])}</u></a>','cell'),cell(s['pages']),cell(appendix_pages.get(s['id'],''),'cellb')] for s in m['sources']],[16*mm,12*mm,22*mm,26*mm,CW-16*mm-12*mm-22*mm-26*mm-14*mm-22*mm,14*mm,22*mm]))
    return st

def _build_body(model,pages,appendix_pages):
    """pages = page numbers known from the previous pass (displayed in the TOC); returns the numbers recorded in this pass."""
    buf=io.BytesIO();recorded={};_Canvas.pages_sink=recorded
    doc=BaseDocTemplate(buf,pagesize=A4,leftMargin=ML,rightMargin=MR,topMargin=MT,bottomMargin=MB,title=f"{model['meta']['title']} · {model['patient']['name']}",author='HealthBook AI',subject=model['meta']['description'],creator='HealthBook AI export',invariant=1)
    doc.addPageTemplates([PageTemplate(id='main',frames=[Frame(ML,MB,CW,H-MT-MB,id='f',leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)],onPage=_decorate(model))])
    doc.build(_story(model,pages,appendix_pages),canvasmaker=_Canvas)
    return buf.getvalue(),doc.page,recorded

def _source_pages(sources):
    """Page count each source will occupy in the appendix (1 divider + its pages)."""
    out=[]
    for s in sources:
        raw=None
        try:raw=decrypt(read_document(_doc_storage_name(s)))
        except Exception:raw=None
        n=1
        if raw and s['mime']=='application/pdf':
            try:
                with fitz.open(stream=raw,filetype='pdf') as d:n=d.page_count
            except Exception:n=1
        out.append((s,raw,n))
    return out
_storage_lookup={}
def _doc_storage_name(s):return _storage_lookup[s['id']]

def _appendix_pages_map(body_pages,prepared,with_sources):
    """Absolute 1-based page of each source's first page in the merged PDF."""
    out={};pg=body_pages
    if not with_sources:return out
    for s,raw,n in prepared:
        pg+=1;out[s['id']]=pg+1;pg+=n  # divider page then the document pages
    out['_total_pages']=pg;return out

def _append_sources(doc,model,prepared,body_pages):
    """Append each original document after a divider page. PDFs are placed as vector pages, images at full resolution."""
    fontfile=os.path.join(FONT_DIR,'DejaVuSans.ttf');fontfile_b=os.path.join(FONT_DIR,'DejaVuSans-Bold.ttf')
    paper=(0.984,0.973,0.949);teal=(0.122,0.373,0.42);ink=(0.137,0.188,0.227);muted=(0.4,0.447,0.486)
    head=f"HealthBook AI · {model['patient']['name']} · generated {model['meta']['generated_display']}"
    first_pages={}
    for s,raw,n in prepared:
        # divider
        pg=doc.new_page(width=W,height=H);pg.draw_rect(pg.rect,color=None,fill=paper)
        pg.draw_rect(fitz.Rect(ML,70,W-MR,73),color=None,fill=teal)
        pg.insert_text((ML,100),f"APPENDIX {s['appendix']}",fontsize=10,fontfile=fontfile_b,fontname='BCB',color=teal)
        pg.insert_textbox(fitz.Rect(ML,112,W-MR,190),s['title'],fontsize=20,fontfile=fontfile_b,fontname='BCB',color=ink)
        rows=[('Reference',s['ref']),('Record date',fmt_d(date.fromisoformat(s['record_date'])) if s['record_date'] else 'undated'),('Category',s['category'].replace('_',' ')),('Pages',str(n)),('Original format',{'application/pdf':'PDF (pages embedded unchanged)','image/jpeg':'JPEG photograph (original resolution)','image/png':'PNG image (original resolution)'}.get(s['mime'],s['mime'])),('Transcription','reviewed by caregiver' if s['reviewed'] else 'automatic, pending review'),('Imported as',s['source_ref'] or '—')]
        y=205
        for k,v in rows:
            pg.insert_text((ML,y),k,fontsize=8,fontfile=fontfile,fontname='BC',color=muted);pg.insert_textbox(fitz.Rect(ML+110,y-9,W-MR,y+30),v,fontsize=9.5,fontfile=fontfile,fontname='BC',color=ink);y+=20
        pg.insert_textbox(fitz.Rect(ML,y+14,W-MR,y+60),'The following page(s) reproduce the original document without modification. Header and footer bands are added outside the document area.',fontsize=8,fontfile=fontfile,fontname='BC',color=muted)
        first_pages[s['id']]=pg.number+1+1  # first content page (1-based)
        _appendix_footer(pg,head,fontfile,muted,teal,s)
        area=fitz.Rect(10*mm,16*mm,W-10*mm,H-16*mm)
        if raw and s['mime']=='application/pdf':
            try:
                with fitz.open(stream=raw,filetype='pdf') as src:
                    for i in range(src.page_count):
                        np_=doc.new_page(width=W,height=H);np_.draw_rect(np_.rect,color=None,fill=(1,1,1));np_.show_pdf_page(area,src,i,keep_proportion=True)
                        _appendix_footer(np_,head,fontfile,muted,teal,s,i+1,src.page_count)
            except Exception:
                np_=doc.new_page(width=W,height=H);np_.insert_textbox(area,'This PDF could not be embedded. The file is available in the archive export.',fontsize=10,fontfile=fontfile,fontname='BC',color=muted);_appendix_footer(np_,head,fontfile,muted,teal,s)
        elif raw:
            np_=doc.new_page(width=W,height=H);np_.draw_rect(np_.rect,color=None,fill=(1,1,1))
            try:np_.insert_image(area,stream=raw,keep_proportion=True)
            except Exception:
                try:
                    pix=fitz.Pixmap(raw);np_.insert_image(area,pixmap=pix,keep_proportion=True)
                except Exception:np_.insert_textbox(area,'This image could not be embedded. The file is available in the archive export.',fontsize=10,fontfile=fontfile,fontname='BC',color=muted)
            _appendix_footer(np_,head,fontfile,muted,teal,s)
        else:
            np_=doc.new_page(width=W,height=H);np_.insert_textbox(area,'The stored file for this document is not available.',fontsize=10,fontfile=fontfile,fontname='BC',color=muted);_appendix_footer(np_,head,fontfile,muted,teal,s)
    return first_pages
def _appendix_footer(pg,head,fontfile,muted,teal,s,i=None,n=None):
    pg.insert_text((ML,10*mm),head,fontsize=6.6,fontfile=fontfile,fontname='BC',color=muted)
    label=f"Appendix {s['appendix']} · {s['ref']} · {s['title'][:60]}"+(f" · page {i} of {n}" if i else '')
    tw=fitz.Font(fontfile=fontfile).text_length(label,fontsize=6.6);pg.insert_text((W-MR-tw,10*mm),label,fontsize=6.6,fontfile=fontfile,fontname='BC',color=muted)

def _finish(body_pdf,model,prepared,with_sources,pages):
    doc=fitz.open(stream=body_pdf,filetype='pdf');body_n=doc.page_count
    first_pages=_append_sources(doc,model,prepared,body_n) if with_sources else {}
    # resolve bcsrc: links → GOTO appendix page; collect back-targets (first body page that cites each source)
    back={};summary_page=pages.get('summary',2)
    for pno in range(body_n):
        page=doc[pno]
        for l in page.get_links():
            uri=l.get('uri') or ''
            if l['kind']==fitz.LINK_URI and uri.startswith('bcsrc:'):
                _,did,pg=uri.split(':');did=int(did);pg=int(pg or 1)
                page.delete_link(l)
                if did in first_pages:
                    target=first_pages[did]-1+(pg-1);target=max(0,min(target,doc.page_count-1))  # page 0 = the divider page
                    page.insert_link({'kind':fitz.LINK_GOTO,'from':l['from'],'page':target,'to':fitz.Point(0,0)});back.setdefault(did,pno)
    # back links on every appendix page
    if with_sources:
        fontfile=os.path.join(FONT_DIR,'DejaVuSans.ttf');pno=body_n
        for s,raw,n in prepared:
            tgt=back.get(s['id'],summary_page-1)
            for k in range(1+n):
                page=doc[pno+k];r=fitz.Rect(W-MR-70,H-12*mm,W-MR,H-6*mm)
                page.insert_text((r.x0+4,r.y1-4),'‹ Back to summary',fontsize=6.6,fontfile=fontfile,fontname='BC',color=(0.122,0.373,0.42))
                page.insert_link({'kind':fitz.LINK_GOTO,'from':r,'page':tgt,'to':fitz.Point(0,0)})
            pno+=1+n
    # page X of Y on every page (outside the document area on appendix pages)
    total=doc.page_count;fontfile=os.path.join(FONT_DIR,'DejaVuSans.ttf')
    for i,page in enumerate(doc):
        if i==0:continue
        page.insert_text((ML,H-8*mm),f"Page {i+1} of {total}",fontsize=6.8,fontfile=fontfile,fontname='BC',color=(0.4,0.447,0.486))
    # bookmarks
    toc=[[1,'Cover',1],[1,'Executive clinical summary',pages.get('summary',2)],[1,'Contents',pages.get('toc',3)]]
    order=[('timeline','Medical timeline'),('hosp','Hospitalizations'),('ill','Illness episodes'),('meds','Medications'),('abx','Antibiotic history'),('labs','Laboratory results'),('micro','Microbiology and E. coli'),('vax','Vaccinations'),('growth','Growth'),('care','Daily Health journal'),('pending','Items pending verification'),('srcidx','Original source documents (index)')]
    for key,title in order:
        if key in pages:toc.append([1,title,pages[key]])
        if key=='hosp':toc+=[[2,f"{fmt_d(date.fromisoformat(h['admitted']))} · {h['facility']}",pages[f"hosp{h['id']}"]] for h in model['hospitalizations'] if f"hosp{h['id']}" in pages]
        if key=='labs':toc+=[[2,g['category'],pages[f'lab_{i}']] for i,g in enumerate(model['labs']) if f'lab_{i}' in pages]
    if with_sources and prepared:
        toc.append([1,'Appendix: original documents',body_n+1])
        for s,raw,n in prepared:toc.append([2,f"Appendix {s['appendix']} · {s['ref']} · {s['title'][:70]}",first_pages[s['id']]-1])
    toc=[[l,t,max(1,min(int(p),total))] for l,t,p in toc]
    doc.set_toc(toc)
    gen=datetime.fromisoformat(model['meta']['generated']);stamp=gen.strftime("D:%Y%m%d%H%M%S")+("Z" if gen.utcoffset() is None else gen.strftime('%z')[:3]+"'"+gen.strftime('%z')[3:]+"'")
    doc.set_metadata({'title':f"{model['meta']['title']} · {model['patient']['name']}",'author':'HealthBook AI','subject':model['meta']['description'],'creator':'HealthBook AI export','producer':'HealthBook AI','creationDate':stamp,'modDate':stamp})
    out=doc.tobytes(garbage=3,deflate=True,clean=False,no_new_id=True);doc.close();return out,total

# ------------------------------------------------------------------ public API
def render(db,p,preset,custom=None,mode='with_sources',generated=None,model=None):
    """Return (bytes, filename, info). mode: summary | with_sources | archive."""
    model=model or build_model(db,p,preset,custom,generated)
    from .models import Document
    for s in model['sources']:
        d=db.get(Document,s['id']);_storage_lookup[s['id']]=d.storage_name
    with_sources=mode in('with_sources','archive') and model['meta']['include_sources'] and bool(model['sources'])
    prepared=_source_pages(model['sources']) if with_sources else []
    # pass 1: learn body page numbers; pass 2: render with TOC numbers and appendix pages; pass 3 if pagination moved
    body,n,pages=_build_body(model,{},{})
    for _ in range(3):
        amap=_appendix_pages_map(n,prepared,with_sources);body,n2,pages2=_build_body(model,pages,amap)
        if n2==n and pages2==pages:break
        pages,n=pages2,n2
    pdf,total=_finish(body,model,prepared,with_sources,pages)
    info={'pages':total,'body_pages':n,'sources_appended':len(prepared),'source_pages':sum(x[2] for x in prepared),'filename':model['meta']['filename']+'.pdf','counts':model['counts'],'title':model['meta']['title']}
    if mode!='archive':return pdf,info['filename'],info
    zbuf=io.BytesIO()
    with zipfile.ZipFile(zbuf,'w',zipfile.ZIP_DEFLATED) as z:
        zi=lambda name:zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0))
        z.writestr(zi('Medical_Record.pdf'),pdf)
        man=io.StringIO();w=csv.writer(man);w.writerow(['file','appendix','reference','record_date','category','title','pages','mime','appendix_page'])
        for (s,raw,n_),src in zip(prepared,model['sources']):
            if raw:z.writestr(zi('Original_Reports/'+s['file']),raw)
            w.writerow(['Original_Reports/'+s['file'],s['appendix'],s['ref'],s['record_date'] or '',s['category'],s['title'],n_,s['mime'],amap.get(s['id'],'')])
        z.writestr(zi('manifest.csv'),man.getvalue());z.writestr(zi('Record_Summary.txt'),export_text(model))
    info['filename']=model['meta']['filename']+'.zip';return zbuf.getvalue(),info['filename'],info

def preview(db,p,preset,custom=None):
    model=build_model(db,p,preset,custom)
    return {'title':model['meta']['title'],'description':model['meta']['description'],'filename':model['meta']['filename'],'date_range':model['meta']['date_range'],'sections':model['meta']['sections'],'counts':model['counts'],'estimated_pages':estimate_pages(model),'estimated_pages_summary_only':estimate_pages({**model,'meta':{**model['meta'],'sections':[s for s in model['meta']['sections'] if s!='sources']}}),'sources':[{'ref':s['ref'],'appendix':s['appendix'],'title':s['title'],'date':s['record_date'],'category':s['category'],'pages':s['pages']} for s in model['sources']],'pending':model['counts']['pending']}

def build_share_summary_pdf(db,p,sel,entries,generated,care_section=None):
    """Drop-in for share.build_summary_pdf: same signature, renders the premium body (no appendix; the ZIP carries originals)."""
    from .export_model import ALL_SECTIONS
    preset={'ecoli':'ecoli','latest_hospitalization':'latest_hospitalization','antibiotics':'antibiotics','vaccination':'vaccination','complete':'complete'}.get(sel.get('preset') or '','custom')
    custom={'sections':list(ALL_SECTIONS),'include_sources':False,'date_from':sel.get('date_from'),'date_to':sel.get('date_to'),'daily_care_days':sel.get('daily_care_days')} if preset=='custom' else None
    pdf,_,_=render(db,p,preset,custom,'summary',generated);return pdf
