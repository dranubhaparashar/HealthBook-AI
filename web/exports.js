/* Download / Share record: quick export tiles, custom selection, three output modes, preview, server-side PDF dossier.
   Every "Download" / "Doctor summary" / "Export" button in the app opens this page. Requests happen only on user events. */
Object.assign(S,{ex:{preset:'complete',mode:'with_sources',sections:new Set(['summary','timeline','hospitalizations','illnesses','prescriptions','medicines','antibiotics','labs','cultures','vaccinations','growth','daily_care','sources']),photos:false,date_from:'',date_to:'',care_days:7,preview:null,previewKey:'',loading:false,busy:false,error:'',done:null}});
NAV.splice(NAV.findIndex(([k])=>k==='settings'),0,['export','Download / Share']);
ICONS.export=`<svg width="21" height="21" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/></svg>`;
const EX_TILES=[
 ['complete','Complete Medical Record','Everything: summary, timeline, hospitalizations, medicines, antibiotics, labs, cultures, vaccinations, growth and every original report.'],
 ['ecoli','E. coli History','Culture reports naming E. coli with the linked illness, hospitalization, prescriptions, antibiotic courses and laboratory reports.'],
 ['latest_hospitalization','Latest Hospitalization','The most recent admission with its discharge summary and every record dated inside the stay.'],
 ['antibiotics','Antibiotic History','Every documented antibiotic course and prescription, with sources and administration status.'],
 ['labs','Laboratory History','All laboratory and culture reports with transcribed values, trends and reference intervals.'],
 ['vaccination','Vaccination Record','All vaccination card entries with status; due dates are never shown as given.'],
 ['daily_care_7d','Daily Care · last 7 days','Fluids, diapers, stool entries, medicines actually given, symptoms, sleep and activity you recorded.'],
 ['custom','Custom','Choose sections, categories and a date range yourself.']];
const EX_SECTIONS=[['summary','Executive summary','Hospitalizations, episodes, medicines, antibiotics, key labs, cultures, vaccination status'],['timeline','Medical timeline','Every documented event in date order'],['hospitalizations','Hospitalizations','Admission, diagnosis, treatment, discharge prescription, follow-up'],['illnesses','Illness episodes','Episode cards with fever, antibiotics and labs in window'],['prescriptions','Prescriptions','Outpatient and discharge prescriptions (status PRESCRIBED)'],['medicines','Medicines','Full medication table with administration status'],['antibiotics','Antibiotic history','Courses, metrics, course timeline'],['labs','Laboratory results','Categorised tables with each report\'s own reference interval'],['cultures','Cultures','Microbiology and susceptibility comparison'],['vaccinations','Vaccinations','Card entries; due ≠ given'],['growth','Growth','Documented measurements'],['daily_care','Daily Care journal','Parent journal for the chosen days'],['stool_photos','Stool photographs','Only if you tick this; never included by default'],['sources','Original source reports','Append every original document as an appendix']];
const EX_MODES=[['summary','PDF summary','Designed medical summary only.'],['with_sources','PDF + original reports','Summary with every original document appended, with clickable source references.'],['archive','Complete ZIP archive','Medical_Record.pdf, Original_Reports/ as separate files, manifest.csv.']];
function exSpec(){const x=S.ex;const spec={preset:x.preset,mode:x.mode,include_photos:x.photos&&(x.preset==='daily_care_7d'||x.sections.has('daily_care'))};
 if(x.preset==='custom'){spec.sections=[...x.sections].filter(k=>k!=='stool_photos');spec.include_sources=x.sections.has('sources');spec.include_photos=x.sections.has('stool_photos');if(x.date_from)spec.date_from=x.date_from;if(x.date_to)spec.date_to=x.date_to;if(x.sections.has('daily_care'))spec.daily_care_days=Number(x.care_days)||7;}
 if(x.mode==='summary')spec.include_sources=false;return spec;}
async function exPreview(){if(!S.token||!S.pid)return;const spec=exSpec();const key=S.pid+JSON.stringify(spec);if(S.ex.previewKey===key&&S.ex.preview)return;S.ex.loading=true;S.ex.error='';S.ex.done=null;exPaint();
 try{const pv=await request(`/patients/${S.pid}/export/preview`,{method:'POST',json:spec});if(S.pid+JSON.stringify(exSpec())!==key)return;S.ex.preview=pv;S.ex.previewKey=key;}catch(e){S.ex.error=e.message||String(e);S.ex.preview=null;S.ex.previewKey='';}
 S.ex.loading=false;exPaint();}
async function exDownload(){if(S.ex.busy)return;const spec=exSpec();S.ex.busy=true;S.ex.error='';S.ex.done=null;exPaint();
 try{const res=await request(`/patients/${S.pid}/export/download`,{method:'POST',json:spec});const blob=await res.blob();const cd=res.headers.get('Content-Disposition')||'';const m=cd.match(/filename="?([^";]+)"?/);const name=m?m[1]:(S.ex.preview?.filename||'Medical_Record.pdf');
  const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);S.ex.done={name,size:blob.size};toast(`${name} downloaded`);}
 catch(e){S.ex.error=e.message||String(e);}
 S.ex.busy=false;exPaint();}
function exPaint(){if(S.tab!=='export')return;const node=el('#ex-preview');if(node)node.innerHTML=exPreviewHtml();const btn=el('#ex-download');if(btn)btn.disabled=S.ex.busy||S.ex.loading||!S.ex.preview;}
const exFmt=d=>d?new Date(d+'T12:00:00').toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}):'—';
function exPreviewHtml(){const x=S.ex,pv=x.preview;
 if(x.loading)return `<div class="ex-busy">Preparing preview…</div>`;
 if(x.error)return `<div class="alert">${esc(x.error)}</div><div class="ex-actions"><button class="btn alt" data-action="ex-refresh">Try again</button></div>`;
 if(!pv)return `<div class="empty">Choose an export to see what it will contain.</div>`;
 const c=pv.counts||{};const r=pv.date_range||{};const modeTitle=EX_MODES.find(m=>m[0]===x.mode)[1];
 return `<h2>${esc(pv.title)}</h2><div class="ex-file">${esc(pv.filename)}</div>
 <div class="ex-range"><b>${esc(exFmt(r.from))}</b> to <b>${esc(exFmt(r.to))}</b> <span class="muted tiny">${r.derived?'(span of documented events)':'(selected range)'}</span></div>
 <div class="ex-secs">${(pv.sections||[]).map(s=>`<i>${esc(s.replace('_',' '))}</i>`).join('')}</div>
 <div class="ex-counts"><div class="ex-count"><b>${c.sources??0}</b><span>source reports</span></div><div class="ex-count"><b>${c.prescriptions??0}</b><span>prescriptions</span></div><div class="ex-count"><b>${c.lab_documents??0}</b><span>lab reports</span></div><div class="ex-count"><b>${c.hospitalizations??0}</b><span>hospitalizations</span></div><div class="ex-count"><b>${c.antibiotic_courses??0}</b><span>antibiotic courses</span></div><div class="ex-count"><b>≈${pv.estimated_pages??'—'}</b><span>estimated pages</span></div></div>
 ${c.pending?`<p class="muted tiny">${c.pending} transcribed item(s) are still pending caregiver verification; they are marked as such in the PDF.</p>`:''}
 ${x.mode!=='summary'&&pv.sources?.length?`<details><summary>${pv.sources.length} original document(s) will be appended</summary><div class="ex-sources">${pv.sources.map(s=>`<div><b>${esc(s.appendix)}</b><span>${esc(exFmt(s.date))}</span><span>${esc(s.title)}</span></div>`).join('')}</div></details>`:''}
 ${x.done?`<div class="headnote" style="margin:12px 0 0">Downloaded <b>${esc(x.done.name)}</b> (${(x.done.size/1048576).toFixed(1)} MB).</div>`:''}
 <div class="ex-actions"><button class="btn" id="ex-download" data-action="ex-download" ${x.busy?'disabled':''}>${x.busy?'Generating…':`Download ${esc(modeTitle)}`}</button><button class="btn alt" data-action="ex-refresh">Refresh preview</button></div>
 <p class="ex-privacy">Generated on the server for this request only and never stored. The file carries no account tokens or links back to your account; share it as you would any medical document.</p>`;}
function exportView(){const x=S.ex;const p=S.patients.find(q=>q.id===S.pid);
 const tiles=EX_TILES.map(([k,t,d])=>`<button class="ex-tile ${x.preset===k?'active':''}" data-action="ex-preset" data-preset="${k}"><span class="ex-kicker">${k==='custom'?'Build your own':'Quick export'}</span><b>${esc(t)}</b><span>${esc(d)}</span></button>`).join('');
 const custom=x.preset==='custom'?`<div class="card" style="margin-top:14px"><h3>What to include</h3><div class="ex-checks">${EX_SECTIONS.map(([k,t,d])=>`<label><input type="checkbox" data-action="ex-section" data-key="${k}" ${x.sections.has(k)?'checked':''}/><span>${esc(t)}<small>${esc(d)}</small></span></label>`).join('')}</div>
  <div class="ex-dates"><label class="field">From<input type="date" id="ex-from" value="${esc(x.date_from)}" data-action="ex-date"/></label><label class="field">To<input type="date" id="ex-to" value="${esc(x.date_to)}" data-action="ex-date"/></label></div>
  ${x.sections.has('daily_care')?`<label class="field" style="max-width:220px">Daily Care days<input type="number" min="1" max="365" id="ex-days" value="${x.care_days}" data-action="ex-date"/></label>`:''}
  <p class="muted tiny">Leave the dates empty for the whole record. Stool photographs are included only when ticked above.</p></div>`:'';
 const photos=x.preset==='daily_care_7d'?`<div class="card" style="margin-top:14px"><label class="check"><input type="checkbox" data-action="ex-photos" ${x.photos?'checked':''}/> <span>Include stool photographs in this export <small class="muted">(off by default)</small></span></label></div>`:'';
 const modes=EX_MODES.map(([k,t,d])=>`<label class="ex-mode ${x.mode===k?'active':''}"><input type="radio" name="ex-mode" value="${k}" data-action="ex-mode" ${x.mode===k?'checked':''}/><span><b>${esc(t)}${k==='with_sources'?'<span class="ex-default">Default</span>':''}</b><span>${esc(d)}</span></span></label>`).join('');
 return `${header('DOWNLOAD / SHARE RECORD',`${esc(p?.name||'This profile')}’s medical record as a PDF`,'A designed dossier: cover, executive summary, contents with page numbers, timeline, medications with their documented status, labs by category, microbiology, vaccinations, and the original reports appended with clickable references.')}
 <div class="ex-tiles">${tiles}</div>
 <div class="ex-grid"><div>${custom}${photos}<div class="card" style="margin-top:14px"><h3>Output</h3><div class="ex-modes">${modes}</div></div>
 <div class="card" style="margin-top:14px"><h3>What the PDF never does</h3><ul class="muted" style="font-size:13px;margin:0;padding-left:18px;line-height:1.7"><li>Turn a prescription into a record of administration — statuses are PRESCRIBED, SCHEDULED, CONFIRMED GIVEN or UNKNOWN exactly as documented.</li><li>Count culture susceptibility antibiotics as medicines given.</li><li>Show a vaccination due date as a given date.</li><li>Replace an original report with its transcription — every source page is appended unchanged.</li></ul></div></div>
 <div class="card ex-preview" id="ex-preview">${exPreviewHtml()}</div></div>`;}
/* ---- wire into the app ---- */
const exPrevRender=render;render=function(){if(S.tab==='export'&&S.token&&S.patients.length){el('#app').innerHTML=layout(exportView());return;}return exPrevRender.apply(this,arguments);};
const exPrevExecute=execute;execute=async function(action,id,b){const x=S.ex;
 switch(action){
  case'ex-preset':x.preset=b.dataset.preset;x.preview=null;x.previewKey='';render();exPreview();return;
  case'ex-mode':x.mode=b.value;render();exPreview();return;
  case'ex-section':{const k=b.dataset.key;if(b.checked)x.sections.add(k);else x.sections.delete(k);x.preview=null;render();exPreview();return;}
  case'ex-photos':x.photos=b.checked;x.preview=null;exPreview();return;
  case'ex-date':x.date_from=el('#ex-from')?.value||'';x.date_to=el('#ex-to')?.value||'';x.care_days=Number(el('#ex-days')?.value)||7;x.preview=null;exPreview();return;
  case'ex-refresh':x.preview=null;x.previewKey='';exPreview();return;
  case'ex-download':return exDownload();
  case'ex-open':x.preset=b.dataset.preset||'complete';x.mode='with_sources';x.preview=null;x.previewKey='';S.tab='export';render();window.scrollTo({top:0});exPreview();return;
  case'summary':case'full-export':x.preset='complete';x.mode='with_sources';x.preview=null;x.previewKey='';S.tab='export';render();window.scrollTo({top:0});exPreview();return;
 }
 return exPrevExecute.call(this,action,id,b);};
document.addEventListener('change',e=>{const t=e.target;if(t&&t.dataset&&t.dataset.action&&t.dataset.action.startsWith('ex-')&&t.dataset.action!=='ex-download'){execute(t.dataset.action,t.dataset.id,t).catch(fail);}
 if(t&&t.id==='destination'&&t.value==='export')setTimeout(exPreview,0);});
document.addEventListener('click',e=>{const b=e.target.closest('[data-tab=export]');if(b)setTimeout(exPreview,0);},true);
