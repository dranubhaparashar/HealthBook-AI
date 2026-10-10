const {test,expect}=require('@playwright/test');
const path=require('path');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`share-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}
async function api(page,method,url,data){const r=await page.request.fetch(url,{method,data,headers:{'X-Requested-With':'BabyCare','Content-Type':'application/json'}});expect(r.ok(),`${method} ${url} ${await r.text()}`).toBeTruthy();return r.json();}

test('share with doctor: wizard preview, zip download, secure link, doctor page, revoke',async({page,context})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 const pid=(await api(page,'GET','/api/patients'))[0].id;
 const up=async(ref,category,date)=>{const r=await page.request.post(`/api/patients/${pid}/documents`,{headers:{'X-Requested-With':'BabyCare'},multipart:{file:{name:`${ref}.pdf`,mimeType:'application/pdf',buffer:Buffer.concat([require('fs').readFileSync(path.resolve('.cache/fixtures/vaccination.pdf')),Buffer.from('%'+ref)])},category,title:`Synthetic ${ref}`,source_ref:ref,...(date?{record_date:date}:{})}});expect(r.ok(),await r.text()).toBeTruthy();return (await r.json()).id;};
 const culture=await up('CUL_01','culture','2026-08-20');const rx=await up('RX_01','prescription','2026-08-21');const other=await up('OTH_01','prescription','2026-02-24');
 const obs=await api(page,'POST',`/api/patients/${pid}/observations`,{test_name:'Stool culture',value:'growth',unit:'',specimen:'stool',record_date:'2026-08-20',organism:'Escherichia coli',culture_findings:'Significant growth of E. coli',source_document_id:culture,verified:true});
 const med=await api(page,'POST',`/api/patients/${pid}/medicines`,{name:'Cefixime syrup',generic_name:'cefixime',instructions:'Synthetic 5 day course',prescription_date:'2026-08-21',source_document_id:rx});
 await api(page,'POST',`/api/patients/${pid}/episodes`,{title:'AGE with E. coli on stool culture',started_on:'2026-08-19',ended_on:'2026-08-26',symptoms:'loose stools',documented_diagnosis:'AGE',links:[{kind:'observation',id:obs.id},{kind:'medicine',id:med.id},{kind:'document',id:culture},{kind:'document',id:rx}],source_document_id:culture});
 await page.locator('nav [data-tab=share]').click();
 await expect(page.getByRole('heading',{name:'Share with Doctor'})).toBeVisible();
 await page.getByRole('button',{name:/Share E\. coli history/}).click();
 await expect(page.locator('.sh-step.active')).toContainText('Choose records');
 await page.getByRole('button',{name:'Preview package →'}).click();
 await expect(page.locator('.sh-step.active')).toContainText('Preview');
 await expect(page.locator('.sh-doc')).toHaveCount(2);
 await expect(page.locator('.sh-doc')).toContainText(['Synthetic CUL_01','Synthetic RX_01']);
 await expect(page.getByText('Synthetic OTH_01')).toHaveCount(0);
 await page.screenshot({path:'.cache/screenshots/share-preview.png',fullPage:true});
 await page.getByRole('button',{name:'Continue →'}).click();
 const dl=page.waitForEvent('download');await page.getByRole('button',{name:/Download ZIP package/}).click();const file=await dl;expect(file.suggestedFilename()).toMatch(/^HealthBook_Share_\d{4}-\d{2}-\d{2}\.zip$/);
 await expect(page.getByText('Package ZIP saved')).toBeVisible();
 await page.getByRole('button',{name:'Back to outputs'}).click();
 await page.locator('[data-sh=pin]').fill('2468');
 await page.locator('label:has([data-sh=expiry][value="3d"])').click();
 await page.getByRole('button',{name:'Create secure link'}).click();
 await expect(page.getByRole('heading',{name:'Secure doctor link ready'})).toBeVisible();
 const url=await page.locator('#sh-url').textContent();
 expect(url).toMatch(/\/doctor\.html#t=[A-Za-z0-9_-]{40,}$/);
 await page.screenshot({path:'.cache/screenshots/share-link.png',fullPage:true});
 // doctor opens the link in a fresh, signed-out context
 const doctor=await context.browser().newContext();const dp=await doctor.newPage();
 await dp.goto(url);
 await expect(dp.getByRole('heading',{name:'Enter the PIN'})).toBeVisible();
 await dp.locator('#pin-form input').fill('0000');await dp.locator('#pin-form button').click();
 await expect(dp.getByText('Incorrect PIN')).toBeVisible();
 await dp.locator('#pin-form input').fill('2468');await dp.locator('#pin-form button').click();
 await expect(dp.getByRole('heading',{name:'Fictional Test Child'})).toBeVisible();
 await expect(dp.locator('.doc-row')).toHaveCount(2);
 await expect(dp.getByText('Synthetic OTH_01')).toHaveCount(0);
 await dp.screenshot({path:'.cache/screenshots/share-doctor.png',fullPage:true});
 const forbidden=await dp.request.get(`/api/patients/${pid}/documents`);expect(forbidden.status()).toBe(401);
 await doctor.close();
 // parent revokes; doctor is locked out
 await page.getByRole('button',{name:'Share something else'}).click();
 await expect(page.getByRole('heading',{name:'Active doctor links'})).toBeVisible();
 page.once('dialog',d=>d.accept());
 await page.getByRole('button',{name:'Revoke'}).first().click();
 await expect(page.getByText('revoked').first()).toBeVisible();
 const gone=await page.request.get(url.replace('/doctor.html#t=','/api/share/public/'),{headers:{'X-Share-Pin':'2468'}});expect(gone.status()).toBe(410);
 expect(errors).toEqual([]);
});
