/* Public doctor page: token from the URL fragment (never sent to the server in the page request), optional PIN,
   package-scoped document access by index. No login, no access to anything outside the package. */
(function(){
 const CFG=window.BABYCARE_CONFIG||{};const API=(String(CFG.apiBase||'').replace(/\/$/,''))+'/api';
 const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
 const root=document.getElementById('doctor');
 const token=(new URLSearchParams(location.hash.replace(/^#/,''))).get('t')||'';
 let pin='';let pkg=null;
 const fmt=d=>d?new Date(String(d).slice(0,10)+'T12:00:00').toLocaleDateString('en-IN',{day:'2-digit',month:'short',year:'numeric'}):'undated';
 const headers=()=>pin?{'X-Share-Pin':pin}:{};
 async function load(){
  if(!token){root.innerHTML='<div class="err">This link is incomplete. Ask the family to send it again.</div>';return;}
  let res;try{res=await fetch(`${API}/share/public/${encodeURIComponent(token)}`,{headers:headers(),cache:'no-store'});}catch{root.innerHTML='<div class="err">Cannot reach the records server. The free server may take a minute to wake up; try again shortly.</div>';return;}
  if(res.status===401){askPin(pin?'Incorrect PIN. Try again.':'This package is protected with a PIN given to you by the family.');return;}
  if(res.status===410||res.status===404){root.innerHTML=`<div class="err">${esc((await res.json().catch(()=>({detail:'This link is no longer available.'}))).detail)}</div>`;return;}
  if(res.status===429){root.innerHTML='<div class="err">Too many PIN attempts. Please wait a few minutes.</div>';return;}
  if(!res.ok){root.innerHTML='<div class="err">The package could not be opened.</div>';return;}
  pkg=await res.json();renderPkg();
 }
 function askPin(message){root.innerHTML=`<div class="pc-card"><h2 style="margin-top:0">Enter the PIN</h2><p class="pc-muted pc-small">${esc(message)}</p><form class="pin" id="pin-form"><input type="password" inputmode="numeric" autocomplete="one-time-code" maxlength="12" pattern="\\d*" aria-label="PIN" autofocus/><button class="btn">Open package</button></form></div>`;
  document.getElementById('pin-form').addEventListener('submit',e=>{e.preventDefault();pin=e.target.querySelector('input').value.trim();load();});}
 function renderPkg(){
  const c=pkg.child;const counts=pkg.counts||{};const chips=[['episodes','illness episodes'],['fevers','fever episodes'],['admissions','admissions'],['observations','lab values'],['medicines','prescriptions'],['courses','antibiotic courses'],['vaccines','vaccine entries']].filter(([k])=>counts[k]).map(([k,l])=>`<span class="pc-chip">${counts[k]} ${l}</span>`).join(' ');
  root.innerHTML=`<div class="pc-card"><span class="pc-kicker">Shared medical records</span><h1 style="margin:6px 0 4px;font-size:26px;letter-spacing:-.6px">${esc(c.name)}</h1><p class="pc-muted" style="margin:0 0 10px">Date of birth ${esc(fmt(c.dob))} · age ${esc(c.age)} · <b>${esc(pkg.reason)}</b></p><div class="pc-chips">${chips}</div><div class="pc-note" style="margin-top:12px">${esc(pkg.notice)} Link expires ${esc(fmt(pkg.expires_at))} ${esc(String(pkg.expires_at).slice(11,16))}.</div>
   <div class="pc-actions" style="margin-top:14px">${pkg.include_summary?`<button class="btn" data-open="summary">Open clinician summary (PDF)</button>`:''}${pkg.allow_download&&!pkg.view_only?`<a class="btn alt" href="${API}/share/public/${encodeURIComponent(token)}/package.zip" data-dl="zip">Download full package (ZIP)</a>`:'<span class="pc-chip">View only</span>'}</div></div>
   <div class="pc-card" style="margin-top:14px"><h3 style="margin-top:0">Documents (${pkg.documents.length})</h3><div class="doc-grid">${pkg.documents.map(d=>`<div class="doc-row"><div><div class="t">${esc(d.title)}</div><div class="m">${esc(fmt(d.record_date))} · ${esc(d.category.replaceAll('_',' '))}${d.ref?' · '+esc(d.ref):''} · ${d.pages} page${d.pages>1?'s':''}</div></div><div class="pc-actions"><button class="btn alt" data-open="${d.n}">View</button>${pkg.allow_download&&!pkg.view_only?`<button class="btn alt" data-save="${d.n}">Save</button>`:''}</div></div>`).join('')||'<p class="pc-muted">No documents in this package; see the summary.</p>'}</div></div>`;
  root.querySelectorAll('[data-open]').forEach(b=>b.addEventListener('click',()=>open(b.dataset.open)));
  root.querySelectorAll('[data-save]').forEach(b=>b.addEventListener('click',()=>save(b.dataset.save)));
  if(pin){root.querySelectorAll('a[data-dl]').forEach(a=>a.addEventListener('click',async e=>{e.preventDefault();const blob=await fetchBlob(`${API}/share/public/${encodeURIComponent(token)}/package.zip`);if(blob)download(blob,'HealthBook_Share.zip');}));}
 }
 async function fetchBlob(url){const res=await fetch(url,{headers:headers(),cache:'no-store'});if(!res.ok){alert('This file could not be opened ('+res.status+').');return null;}return res.blob();}
 function download(blob,name){const u=URL.createObjectURL(blob);const a=document.createElement('a');a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),20000);}
 async function save(n){const d=pkg.documents.find(x=>String(x.n)===String(n));const blob=await fetchBlob(`${API}/share/public/${encodeURIComponent(token)}/document/${n}`);if(blob)download(blob,`${d.record_date||'undated'}_${d.title.replace(/[^A-Za-z0-9]+/g,'-').slice(0,40)}${d.mime==='application/pdf'?'.pdf':'.jpg'}`);}
 async function open(n){const url=n==='summary'?`${API}/share/public/${encodeURIComponent(token)}/summary.pdf`:`${API}/share/public/${encodeURIComponent(token)}/document/${n}`;const blob=await fetchBlob(url);if(!blob)return;const obj=URL.createObjectURL(blob);const v=document.createElement('div');v.className='viewer';const isPdf=blob.type==='application/pdf';v.innerHTML=`<header><span>${esc(n==='summary'?'Clinician summary':pkg.documents.find(x=>String(x.n)===String(n))?.title||'Document')}</span><button class="btn alt" id="close-viewer">Close</button></header>${isPdf?`<iframe src="${obj}" title="document"></iframe>`:`<img src="${obj}" alt="document page"/>`}`;document.body.appendChild(v);v.querySelector('#close-viewer').addEventListener('click',()=>{v.remove();URL.revokeObjectURL(obj);});}
 load();
})();
