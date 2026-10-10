/* HealthBook AI family workspace: age-aware navigation, per-profile module settings and the Vitals / Daily Health page.
   Loads last. Profiles are the server's patient rows; modules only show or hide sections, they never move data. */
Object.assign(S,{pv:{data:null,loading:false,error:'',days:14,key:''}});
const PM_TAB_MODULE={documents:'records',medicines:'medicines',hospital:'hospital',vaccines:'vaccines',growth:'growth',labs:'labs',analytics:'labs',lifetime:'lifetime',care:'care',calendar:'calendar',camera:'camera',share:'share',export:'export',vitals:'vitals'};
function currentProfile(){return S.patients.find(p=>p.id===S.pid)||null;}
function profileModules(){const p=currentProfile();return new Set(p&&Array.isArray(p.modules)?p.modules:Object.values(PM_TAB_MODULE));}
window.profileModules=profileModules;
function profileCareKinds(){const p=currentProfile();return p&&Array.isArray(p.care_kinds)?p.care_kinds:['fluid','diaper','stool','medicine','symptom','activity','sleep'];}
window.profileCareKinds=profileCareKinds;
const pmVisible=([id])=>{const m=PM_TAB_MODULE[id];return !m||profileModules().has(m);};
NAV.splice(Math.max(1,NAV.findIndex(([k])=>k==='calendar')>0?NAV.findIndex(([k])=>k==='calendar'):NAV.findIndex(([k])=>k==='settings')),0,['vitals','Vitals']);
ICONS.vitals=`<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"/></svg>`;
/* age-aware navigation: hide modules the profile has switched off */
const pmPrevLayout=layout;layout=function(content){const all=NAV.slice();const shown=all.filter(pmVisible);NAV.length=0;NAV.push(...shown);try{return pmPrevLayout.call(this,content);}finally{NAV.length=0;NAV.push(...all);}};
const pmPrevRender=render;render=function(){const p=currentProfile();if(p&&S.tab&&!pmVisible([S.tab])&&S.token)S.tab='today';if(S.tab==='vitals'&&S.token&&S.patients.length){const old=el('#pv-form');const kept=old?[...old.elements].filter(x=>x.name).map(x=>[x.name,x.value]):null;const focus=document.activeElement&&document.activeElement.closest&&document.activeElement.closest('#pv-form')?document.activeElement.name:null;el('#app').innerHTML=layout(vitalsView());if(kept){const f=el('#pv-form');for(const [n,v] of kept){const x=f&&f.elements[n];if(x&&x.type!=='hidden')x.value=v;}if(focus&&f&&f.elements[focus])f.elements[focus].focus();}return;}return pmPrevRender.apply(this,arguments);};
/* settings: modules per profile */
const pmPrevSettings=typeof settingsView==='function'?settingsView:null;
settingsView=function(){const base=pmPrevSettings?pmPrevSettings():'';const p=currentProfile();if(!p)return base;const mods=profileModules();const avail=p.available_modules||[];
 const card=`<div class="card section-gap" id="pm-modules"><h2>Modules for ${esc(p.name)}</h2><p class="muted" style="font-size:13px">${esc(p.age_group_label||'')} profile${p.modules_custom?' · custom selection':' · age-based defaults'}. Switching a module off hides it for this profile only; nothing is deleted and no other profile changes.</p><form id="pm-modules-form"><div class="ex-checks">${avail.map(m=>`<label><input type="checkbox" name="modules" value="${m.key}" ${mods.has(m.key)?'checked':''}/><span>${esc(m.title)}<small>${esc(m.description)}</small></span></label>`).join('')}</div><div class="pillrow" style="margin-top:12px"><button class="btn">Save modules</button><button type="button" class="btn alt" data-action="pm-reset-modules">Reset to age defaults</button></div></form></div>`;
 return base.replace('</div></div></div>`'.slice(0,0)+'<div class="card"><h2>Appearance</h2>',card+'<div class="card"><h2>Appearance</h2>')||base+card;};
/* vitals page --------------------------------------------------------------------- */
const PV_FIELDS=[['systolic','Systolic','mmHg'],['diastolic','Diastolic','mmHg'],['heart_rate','Heart rate','bpm'],['spo2','SpO2','%'],['glucose_mg_dl','Glucose','mg/dL'],['weight_kg','Weight','kg'],['temperature_c','Temperature','°C']];
function pvNowLocal(){return typeof localClock==='function'?localClock().slice(0,16):new Date().toISOString().slice(0,16);}
let pvSeq=0;
async function pvLoad(force){if(!S.token||!S.pid)return;const key=`${S.pid}:${S.pv.days}`;if(!force&&S.pv.key===key&&S.pv.data)return;const seq=++pvSeq;S.pv.loading=true;S.pv.error='';pvPaint();
 /* only the newest request may write: an older summary still in flight (e.g. the tab-open load) must never overwrite the one fetched after a save */
 try{const d=await request(`/patients/${S.pid}/vitals/summary?days=${S.pv.days}`);if(seq!==pvSeq)return;S.pv.data=d;S.pv.key=key;}catch(e){if(seq!==pvSeq)return;S.pv.error=e.message||String(e);S.pv.data=null;S.pv.key='';}
 S.pv.loading=false;pvPaint();}
function pvPaint(){if(S.tab!=='vitals')return;const n=el('#pv-body');if(n)n.innerHTML=pvBodyHtml();}
function pvChart(key,label,unit){const d=S.pv.data;if(!d)return '';const pts=[];for(const e of d.entries){if(e[key]!=null)pts.push({date:e.at.slice(0,10),value:e[key],note:e.at.slice(11,16)});}
 if(key==='systolic'){for(const e of d.entries){if(e.diastolic!=null)pts.push({date:e.at.slice(0,10),value:e.diastolic,tag:'diastolic',note:e.at.slice(11,16)});}}
 if(!pts.length)return '';try{if(typeof PC!=='undefined'&&PC.line)return `<div class="card"><h3>${esc(label)}</h3>${PC.line({points:pts,unit,title:label,aria:`${label} readings`,emptyTitle:`No ${label.toLowerCase()} readings`})}</div>`;}catch(e){}
 return `<div class="card"><h3>${esc(label)}</h3><div class="muted tiny">${pts.length} reading(s)</div></div>`;}
function pvBodyHtml(){const x=S.pv;if(x.loading&&!x.data)return `<div class="loading">Loading vitals…</div>`;if(x.error)return `<div class="alert">${esc(x.error)}</div><div class="pillrow section-gap"><button class="btn alt" data-action="pv-retry">Try again</button></div>`;const d=x.data;if(!d)return `<div class="empty">Vitals will appear here once you add a reading.</div>`;
 const latest=d.latest;const rows=d.entries.slice().reverse();
 return `<div class="cards"><div class="card statcard"><div><span class="eyebrow">LATEST BLOOD PRESSURE</span><div class="stat">${latest&&latest.systolic?`${latest.systolic}/${latest.diastolic}`:'—'}</div><span class="muted">mmHg${latest&&latest.systolic?' · '+esc(fmt(latest.at.slice(0,10))):''}</span></div></div><div class="card statcard"><div><span class="eyebrow">LATEST HEART RATE · SpO2</span><div class="stat">${latest&&latest.heart_rate?latest.heart_rate:'—'}<small style="font-size:16px;font-weight:400"> bpm</small> · ${latest&&latest.spo2?latest.spo2+'%':'—'}</div><span class="muted">as recorded</span></div></div><div class="card statcard"><div><span class="eyebrow">LATEST GLUCOSE · WEIGHT</span><div class="stat">${latest&&latest.glucose_mg_dl!=null?latest.glucose_mg_dl:'—'}<small style="font-size:16px;font-weight:400"> mg/dL</small> · ${latest&&latest.weight_kg!=null?latest.weight_kg+' kg':'—'}</div><span class="muted">${esc(latest&&latest.glucose_context?latest.glucose_context.replace('_',' '):'')}</span></div></div></div>
 ${d.statements.length?`<div class="card"><h2>Recorded in the last ${x.days} days</h2>${d.statements.map(s=>`<p style="margin:6px 0">· ${esc(s)}</p>`).join('')}<p class="muted tiny" style="margin-top:10px">${esc(d.warnings[0]||'')}</p></div>`:''}
 <div class="lab-grid section-gap">${pvChart('systolic','Blood pressure','mmHg')}${pvChart('heart_rate','Heart rate','bpm')}${pvChart('spo2','SpO2','%')}${pvChart('glucose_mg_dl','Glucose','mg/dL')}${pvChart('weight_kg','Weight','kg')}${pvChart('temperature_c','Temperature','°C')}</div>
 <div class="card section-gap"><div class="sectionhead"><h2>Entries</h2><div class="pillrow">${[7,14,30,90].map(n=>`<button class="chip ${x.days===n?'active':''}" data-action="pv-days" data-days="${n}">${n} days</button>`).join('')}</div></div>${rows.length?`<div class="tablewrap"><table class="data"><thead><tr><th>When</th><th>Reading</th><th>Note</th><th></th></tr></thead><tbody>${rows.map(e=>`<tr><td>${esc(fmt(e.at.slice(0,10)))} ${esc(e.at.slice(11,16))}</td><td><b>${esc(e.summary)}</b></td><td class="muted">${esc(e.note||'')}</td><td><button class="btn ghost small" data-action="pv-withdraw" data-id="${e.id}">Withdraw</button></td></tr>`).join('')}</tbody></table></div>`:`<div class="empty">No vitals recorded in this range.</div>`}</div>`;}
function vitalsView(){const p=currentProfile();
 return `${header('DAILY HEALTH',`${esc(p?p.name:'')}’s vitals and daily health`,'Blood pressure, heart rate, SpO2, glucose, weight, temperature and meals, recorded by you and shown exactly as entered. Fluids, stool, medicines given, symptoms, sleep and activity live in the Daily Health journal.',`<button class="btn alt" data-tab="care">Open the journal</button>`)}
 <div class="card"><h2>Add a reading</h2><form id="pv-form"><div class="formgrid">${field('When','at','datetime-local',pvNowLocal(),true)}<label class="field">Glucose context<select name="glucose_context"><option value="">—</option><option value="fasting">Fasting</option><option value="post_meal">After a meal</option><option value="random">Random</option><option value="bedtime">Bedtime</option></select></label>${PV_FIELDS.map(([k,l,u])=>field(`${l} (${u})`,k,'number','',false,'step="any" inputmode="decimal"')).join('')}${field('Meal (optional)','meal','text','',false,'maxlength="300" placeholder="e.g. dal, rice, salad"')}${field('Note (optional)','note','text','',false,'maxlength="500"')}</div><div class="formfoot"><button class="btn">Save reading</button></div></form><p class="muted tiny">Readings are kept per profile and never interpreted; share them with the treating clinician.</p></div>
 <div id="pv-body">${pvBodyHtml()}</div>`;}
/* today card for profiles with the vitals module */
const pmPrevToday=todayView;todayView=function(){let h=pmPrevToday.apply(this,arguments);if(!profileModules().has('vitals'))return h;const d=S.pv.data;const latest=d&&d.latest;
 const card=`<div class="card section-gap" id="pm-today-vitals"><div class="sectionhead"><h2>Daily Health · vitals</h2><button class="btn alt small" data-tab="vitals">Add a reading</button></div>${latest?`<p><b>${esc(latest.summary)}</b><br><span class="muted tiny">recorded ${esc(fmt(latest.at.slice(0,10)))} ${esc(latest.at.slice(11,16))}</span></p>`:`<p class="muted">No vitals recorded yet for ${esc(currentProfile()?.name||'this profile')}. Blood pressure, heart rate, SpO2, glucose, weight and meals can be added in a few taps.</p>`}</div>`;
 return h+card;};
/* wiring */
const pmPrevExecute=execute;execute=async function(action,id,b){
 switch(action){
  case'pv-retry':return pvLoad(true);
  case'pv-days':S.pv.days=Number(b.dataset.days)||14;return pvLoad(true);
  case'pv-withdraw':if(!confirm('Withdraw this reading? It stays in the audit history but leaves the journal.'))return;await request(`/vitals/${id}`,{method:'POST'}).catch(()=>null);await request(`/vitals/${id}/withdraw`,{method:'POST'});toast('Reading withdrawn');return pvLoad(true);
  case'pm-reset-modules':{const p=await request(`/patients/${S.pid}/modules`,{method:'PUT',json:{enabled:null}});pmApplyProfile(p);toast('Modules reset to age-based defaults');render();return;}
 }
 return pmPrevExecute.call(this,action,id,b);};
function pmApplyProfile(p){const i=S.patients.findIndex(x=>x.id===p.id);if(i>=0)S.patients[i]=p;}
const pmPrevSubmit=submitForm;submitForm=async function(f){const fid=f.getAttribute('id');
 if(fid==='pv-form'){const fd=new FormData(f);const body={};for(const [k,v] of fd.entries()){if(v!=='')body[k]=v;}body.client_key=(crypto.randomUUID?crypto.randomUUID():String(Date.now())).replace(/-/g,'').slice(0,32);pvSeq++;const r=await request(`/patients/${S.pid}/vitals`,{method:'POST',json:body});toast(r.created===false?'Already saved':'Reading saved');f.reset();const at=f.querySelector('[name=at]');if(at)at.value=pvNowLocal();return pvLoad(true);}
 if(fid==='pm-modules-form'){const enabled=[...f.querySelectorAll('input[name=modules]:checked')].map(i=>i.value);const p=await request(`/patients/${S.pid}/modules`,{method:'PUT',json:{enabled}});pmApplyProfile(p);toast('Modules saved for this profile');render();return;}
 return pmPrevSubmit.call(this,f);};
document.addEventListener('click',e=>{const b=e.target.closest('[data-tab=vitals]');if(b)setTimeout(()=>pvLoad(false),0);},true);
document.addEventListener('change',e=>{const t=e.target;if(t&&t.id==='destination'&&t.value==='vitals')setTimeout(()=>pvLoad(false),0);if(t&&t.id==='patient-select'){S.pv.data=null;S.pv.key='';}});
