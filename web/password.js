/* HealthBook AI: forgot password / reset password. The emailed link carries the token in the URL fragment
   (#reset-password=…), which is never sent to any server; it is read once here and removed from the address bar. */
function pwFromHash(){const m=/^#reset-password=([A-Za-z0-9_\-]{20,200})$/.exec(location.hash||'');
 if(!m)return false;S.pwReset={token:m[1],state:'checking',error:''};S.pwForgot=null;history.replaceState(null,'',location.pathname+location.search);return true;}
pwFromHash();
window.addEventListener('hashchange',()=>{if(pwFromHash()){render();pwCheck();}});
Object.assign(S,{pwForgot:null});
function pwCard(inner){return `<div class="authpage"><div class="authcard"><div class="brandrow"><div class="logo">+</div><div class="brandtext"><div class="brand">HealthBook AI</div><small>Your private family health workspace.</small></div></div>${inner}</div></div>`;}
function pwForgotView(){const f=S.pwForgot;
 if(f.sent)return pwCard(`<span class="eyebrow">Check your email</span><h1 style="margin:8px 0 12px">Reset link requested</h1><p class="muted" id="pw-sent">${esc(f.message)}</p><p class="muted tiny">Nothing arrived? Check spam, wait a minute, or request again. Only the newest link works.</p><div class="pillrow" style="margin-top:18px"><button class="btn" data-action="pw-back">Back to sign in</button><button class="btn ghost small" data-action="pw-again">Send again</button></div>`);
 return pwCard(`<span class="eyebrow">Forgot password</span><h1 style="margin:8px 0 12px">Reset your password</h1><p class="muted">Enter the email address you use for HealthBook AI. If it belongs to an account, we email a link to set a new password. Your profiles and records are not changed.</p><form id="pw-forgot-form"><label class="field">Email<input name="email" type="email" autocomplete="username" required maxlength="254" placeholder="you@example.com" value="${esc(f.email||'')}"/></label><button class="btn" style="width:100%" type="submit">Email me a reset link</button></form><div style="margin-top:16px;text-align:center"><button class="btn ghost small" data-action="pw-back">Back to sign in</button></div>`);}
function pwResetView(){const r=S.pwReset;
 if(r.state==='checking')return pwCard(`<div class="loading">Checking your reset link…</div>`);
 if(r.state==='invalid')return pwCard(`<span class="eyebrow">Reset link</span><h1 style="margin:8px 0 12px">This link no longer works</h1><div class="alert">${esc(r.error)}</div><div class="pillrow" style="margin-top:18px"><button class="btn" data-action="pw-forgot">Request a new link</button><button class="btn ghost small" data-action="pw-back">Back to sign in</button></div>`);
 if(r.state==='done')return pwCard(`<span class="eyebrow">Password changed</span><h1 style="margin:8px 0 12px">You can sign in now</h1><p class="muted" id="pw-done">${esc(r.message)}</p><p class="muted tiny">For your safety, every device that was signed in to this account has been signed out.</p><button class="btn" style="width:100%;margin-top:14px" data-action="pw-back">Sign in</button>`);
 return pwCard(`<span class="eyebrow">Reset link</span><h1 style="margin:8px 0 12px">Choose a new password</h1><p class="muted">At least 12 characters. Your profiles, documents and medical records stay exactly as they are.</p>${r.error?`<div class="alert" id="pw-error">${esc(r.error)}</div>`:''}<form id="pw-reset-form"><label class="field">New password<input name="password" type="password" autocomplete="new-password" minlength="12" maxlength="200" required/></label><label class="field">Confirm new password<input name="password2" type="password" autocomplete="new-password" minlength="12" maxlength="200" required/></label><button class="btn" style="width:100%" type="submit">Set new password</button></form>`);}
async function pwCheck(){const r=S.pwReset;if(!r||r.state!=='checking')return;try{await request('/auth/password-reset/check',{method:'POST',json:{token:r.token}});r.state='form';}catch(e){r.state='invalid';r.error=e.message;r.token='';}render();}
const pwPrevRender=render;render=function(){if(S.pwReset){el('#app').innerHTML=pwResetView();return;}if(!S.token&&S.pwForgot){el('#app').innerHTML=pwForgotView();return;}const out=pwPrevRender.apply(this,arguments);
 if(!S.token){const card=document.querySelector('#auth-form');if(card&&!document.querySelector('[data-action=pw-forgot]')){const b=document.createElement('div');b.style.cssText='margin-top:8px;text-align:right';b.innerHTML='<button type="button" class="btn ghost small" data-action="pw-forgot">Forgot password?</button>';card.after(b);}}
 return out;};
const pwPrevExecute=execute;execute=async function(action,id,b){
 switch(action){
  case'pw-forgot':{const email=el('#auth-form [name=email]')?.value||'';S.pwReset=null;S.pwForgot={email};render();return;}
  case'pw-again':S.pwForgot={email:S.pwForgot?.email||''};render();return;
  case'pw-back':S.pwForgot=null;S.pwReset=null;S.register=false;if(S.token){S.token='';sessionStorage.removeItem('babycare_signed_in');sessionStorage.removeItem('babycare_token');}render();return;
 }
 return pwPrevExecute.call(this,action,id,b);};
const pwPrevSubmit=submitForm;submitForm=async function(f){const fid=f.getAttribute('id');
 if(fid==='pw-forgot-form'){const email=f.elements.email.value.trim();const r=await request('/auth/password-reset/request',{method:'POST',json:{email}});S.pwForgot={email,sent:true,message:r.message};render();return;}
 if(fid==='pw-reset-form'){const p1=f.elements.password.value,p2=f.elements.password2.value;const r=S.pwReset;
  if(p1!==p2){r.error='The two passwords do not match.';render();return;}
  try{const res=await request('/auth/password-reset/confirm',{method:'POST',json:{token:r.token,password:p1}});r.state='done';r.token='';r.message=res.message;}
  catch(e){if(e.status===400){r.state='invalid';r.token='';}r.error=e.message;}
  render();return;}
 return pwPrevSubmit.call(this,f);};
if(S.pwReset)setTimeout(pwCheck,0);
