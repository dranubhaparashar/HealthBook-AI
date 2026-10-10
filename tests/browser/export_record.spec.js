const {test,expect}=require('@playwright/test');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`export-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}
async function api(page,method,url,data){const r=await page.request.fetch(url,{method,data,headers:{'X-Requested-With':'BabyCare','Content-Type':'application/json'}});expect(r.ok(),`${method} ${url} ${await r.text()}`).toBeTruthy();return r.json();}
const path=require('path');const fs=require('fs');
function fixturePdf(){const f=path.resolve('.cache/fixtures/vaccination.pdf');if(fs.existsSync(f))return fs.readFileSync(f);
 // minimal single-page PDF when the shared fixture is absent
 return Buffer.from(['%PDF-1.4','1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj','2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj','3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]>>endobj','trailer<</Root 1 0 R>>','%%EOF',''].join(String.fromCharCode(10)));}

test('download / share record: tiles, preview counts, selective E. coli export, PDF download, old buttons redirect',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 const pid=(await api(page,'GET','/api/patients'))[0].id;
 const up=async(ref,category,date,seed)=>{const r=await page.request.post(`/api/patients/${pid}/documents`,{headers:{'X-Requested-With':'BabyCare'},multipart:{file:{name:`${ref}.pdf`,mimeType:'application/pdf',buffer:Buffer.concat([fixturePdf(),Buffer.from('%'+ref+seed)])},category,title:`Synthetic ${category} ${ref}`,source_ref:ref,record_date:date}});expect(r.status(),await r.text()).toBe(201);return (await r.json()).id;};
 const culture=await up('CUL_01','culture','2026-08-20',1);const rx=await up('RX_01','prescription','2026-08-21',2);const other=await up('OTH_01','prescription','2026-02-24',3);
 const obs=await api(page,'POST',`/api/patients/${pid}/observations`,{test_name:'Stool culture',value:'growth',unit:'',specimen:'stool',record_date:'2026-08-20',organism:'Escherichia coli',culture_findings:'Significant growth of E. coli',susceptibility:'cefixime: S; amikacin: S',source_document_id:culture,verified:true});
 const med=await api(page,'POST',`/api/patients/${pid}/medicines`,{name:'Cefixime syrup',generic_name:'cefixime',instructions:'Synthetic 5 day course',prescription_date:'2026-08-21',source_document_id:rx,course_type:'prescription'});
 await api(page,'POST',`/api/patients/${pid}/medicines`,{name:'Paracetamol drops',instructions:'Synthetic SOS',prescription_date:'2026-02-24',source_document_id:other});
 await api(page,'POST',`/api/patients/${pid}/episodes`,{title:'AGE with E. coli on stool culture',started_on:'2026-08-19',ended_on:'2026-08-26',symptoms:'loose stools',documented_diagnosis:'AGE',links:[{kind:'observation',id:obs.id},{kind:'medicine',id:med.id},{kind:'document',id:culture},{kind:'document',id:rx}],source_document_id:culture});
 // the page
 await page.locator('nav [data-tab=export]').click();
 await expect(page.getByRole('heading',{name:/medical record as a PDF/})).toBeVisible();
 await expect(page.locator('.ex-tile')).toHaveCount(8);
 await expect(page.locator('.ex-tile.active b')).toHaveText('Complete Medical Record');
 await expect(page.locator('#ex-preview h2')).toHaveText('Complete Medical Record');
 await expect(page.locator('#ex-preview .ex-file')).toContainText('Fictional_Test_Child_Medical_Record_');
 const count=async(label)=>Number(await page.locator('.ex-count',{hasText:label}).locator('b').innerText());
 expect(await count('source reports')).toBe(3);expect(await count('prescriptions')).toBe(2);
 await expect(page.locator('.ex-mode.active b')).toContainText('PDF + original reports');
 // selective preset
 await page.locator('.ex-tile[data-preset=ecoli]').click();
 await expect(page.locator('#ex-preview h2')).toHaveText('E. coli History');
 expect(await count('source reports')).toBe(2);expect(await count('prescriptions')).toBe(1);
 await page.locator('details summary').click();
 await expect(page.locator('.ex-sources')).toContainText('Synthetic culture CUL_01');
 await expect(page.locator('.ex-sources')).not.toContainText('OTH_01');
 // custom selection shows the checklist; stool photos are off by default
 await page.locator('.ex-tile[data-preset=custom]').click();
 await expect(page.locator('.ex-checks input')).toHaveCount(14);
 expect(await page.locator('.ex-checks input[data-key=stool_photos]').isChecked()).toBeFalsy();
 await page.locator('.ex-checks input[data-key=vaccinations]').uncheck();
 await expect(page.locator('#ex-preview .ex-secs')).not.toContainText('vaccinations');
 // download the summary PDF
 await page.locator('.ex-tile[data-preset=ecoli]').click();
 await page.locator('.ex-mode input[value=summary]').check();
 await expect(page.locator('#ex-download')).toBeEnabled();
 const [dl]=await Promise.all([page.waitForEvent('download'),page.locator('#ex-download').click()]);
 expect(dl.suggestedFilename()).toMatch(/^Fictional_Test_Child_Ecoli_History_\d{4}-\d{2}-\d{2}\.pdf$/);
 const fs=require('fs');const p=await dl.path();const head=fs.readFileSync(p).subarray(0,5).toString();expect(head).toBe('%PDF-');
 await expect(page.locator('#ex-preview')).toContainText('Downloaded');
 await page.screenshot({path:'.cache/screenshots/export-record.png',fullPage:true});
 // legacy "Doctor summary" button now opens this page
 await page.locator('nav [data-tab=timeline]').click();
 await page.getByRole('button',{name:/Doctor summary/}).first().click();
 await expect(page.getByRole('heading',{name:/medical record as a PDF/})).toBeVisible();
 await expect(page.locator('.ex-tile.active b')).toHaveText('Complete Medical Record');
 expect(errors).toEqual([]);
});

test('export page works at phone width',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await onboard(page);
 await page.locator('#destination').selectOption('export');
 await expect(page.getByRole('heading',{name:/medical record as a PDF/})).toBeVisible();
 await expect(page.locator('#ex-preview h2')).toHaveText('Complete Medical Record');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
 await page.screenshot({path:'.cache/screenshots/export-mobile.png',fullPage:true});
});
