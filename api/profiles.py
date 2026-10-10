"""Family-workspace profiles: age groups, per-profile module configuration and the profile JSON shared by web and mobile.

A profile is still a `Patient` row internally (ids, routes and table names are unchanged for compatibility). Modules are
presentation switches only: disabling a module hides it, it never deletes or merges data between profiles.
"""
import json
from datetime import date
from .models import Patient, utcnow

BRAND='HealthBook AI'
TAGLINE='Your private family health workspace.'
ALL_MODULES=[
 ('records','Medical records','Reports, prescriptions and original documents'),
 ('medicines','Medicines & supplements','Prescriptions, schedules and administration records'),
 ('hospital','Hospital & visits','Admissions, visits and discharge summaries'),
 ('labs','Laboratory results','Lab values, cultures and trends'),
 ('vaccines','Vaccinations','Vaccination card entries and due dates'),
 ('growth','Growth','Weight, length and head circumference'),
 ('lifetime','Lifetime Health','Fever and antibiotic history across the whole record'),
 ('care','Daily Health journal','Fluids, stool, medicines given, symptoms, sleep and activity'),
 ('diapers','Diapers & feeding','Diaper changes and feeding entries (infants and toddlers)'),
 ('vitals','Vitals','Blood pressure, heart rate, SpO2, glucose, weight, meals'),
 ('calendar','Care calendar','Day, week and month views of everything recorded'),
 ('camera','Home activity camera','Optional local-first activity events'),
 ('share','Share with doctor','Scoped, expiring doctor links and packages'),
 ('export','Download / export','Designed PDF dossiers and archives'),
]
MODULE_KEYS=[k for k,_,_ in ALL_MODULES]
AGE_GROUPS={'infant':'Infant','toddler':'Toddler','child':'Child','teen':'Teenager','adult':'Adult','senior':'Senior'}
RELATIONSHIPS=['self','child','partner','parent','grandparent','sibling','other']
SEXES=['female','male','other','unspecified']
_BASE={'records','medicines','hospital','labs','lifetime','care','calendar','share','export'}
DEFAULT_MODULES={
 'infant':_BASE|{'vaccines','growth','diapers','camera'},
 'toddler':_BASE|{'vaccines','growth','diapers','camera'},
 'child':_BASE|{'vaccines','growth'},
 'teen':_BASE|{'vaccines','growth','vitals'},
 'adult':_BASE|{'vitals'},      # vaccinations can be switched on per profile
 'senior':_BASE|{'vitals'},
}
CARE_KINDS={'infant':['fluid','diaper','stool','medicine','symptom','activity','sleep'],'toddler':['fluid','diaper','stool','medicine','symptom','activity','sleep']}
_CARE_DEFAULT=['fluid','stool','medicine','symptom','activity','sleep']

def months_between(dob,on):
    m=(on.year-dob.year)*12+on.month-dob.month
    if on.day<dob.day:m-=1
    return max(0,m)
def age_group(dob,on=None):
    on=on or utcnow().date();m=months_between(dob,on)
    if m<12:return 'infant'
    if m<36:return 'toddler'
    if m<13*12:return 'child'
    if m<18*12:return 'teen'
    if m<65*12:return 'adult'
    return 'senior'
def age_label(dob,on=None):
    on=on or utcnow().date();m=months_between(dob,on)
    if m<1:return f"{(on-dob).days} days"
    if m<24:return f"{m} months"
    y=m//12;return f"{y} years" if y<2 or m%12==0 else f"{y} years {m%12} months"
def modules_for(p,on=None):
    group=age_group(p.dob,on);defaults=set(DEFAULT_MODULES[group])
    raw=getattr(p,'modules_json',None)
    if raw:
        try:
            cfg=json.loads(raw)
            chosen={k for k in cfg.get('enabled',[]) if k in MODULE_KEYS}
            if cfg.get('enabled') is not None:return chosen,group,True
        except Exception:pass
    return defaults,group,False
def care_kinds(p,modules,group):
    kinds=list(CARE_KINDS.get(group,_CARE_DEFAULT))
    if 'diapers' not in modules and 'diaper' in kinds:kinds.remove('diaper')
    if 'diapers' in modules and 'diaper' not in kinds:kinds.insert(1,'diaper')
    return kinds
def profile_json(p,on=None):
    modules,group,custom=modules_for(p,on)
    return {'id':p.id,'user_id':p.user_id,'name':p.name,'dob':p.dob.isoformat(),'tz':p.tz,'created_at':p.created_at.isoformat() if p.created_at else None,
        'sex':getattr(p,'sex',None),'relationship':getattr(p,'relationship',None),'age_group':group,'age_group_label':AGE_GROUPS[group],'age_label':age_label(p.dob,on),
        'modules':sorted(modules,key=MODULE_KEYS.index),'modules_custom':custom,'care_kinds':care_kinds(p,modules,group),'available_modules':[{'key':k,'title':t,'description':d} for k,t,d in ALL_MODULES]}
def set_modules(p,enabled):
    enabled=[k for k in enabled if k in MODULE_KEYS]
    p.modules_json=json.dumps({'enabled':enabled})
def reset_modules(p):p.modules_json=None
