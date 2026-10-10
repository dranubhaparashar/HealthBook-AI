// Document Library → extraction review → "Confirm and save to history": field-level errors, page jump, excluded
// suggestions never block, success creates history, transcription-only documents save. Synthetic account and files.
const {test,expect}=require('@playwright/test');
async function onboard(page){
 await page.goto('/');await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`review-${Date.now()}@test.invalid`);await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();}
// minimal valid 3-page PDF with synthetic text (no real data)
function pdf3(tag){const objs=[];const page=i=>`BT /F1 14 Tf 72 720 Td (SYNTHETIC ${tag} PAGE ${i}) Tj ET`;
 const parts=['%PDF-1.4'];const off=[];const add=s=>{off.push(parts.join('\n').length+1);parts.push(s);};
 add('1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj');add('2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R 5 0 R]/Count 3>>endobj');
 for(let i=0;i<3;i++)add(`${3+i} 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Resources<</Font<</F1 9 0 R>>>>/Contents ${6+i} 0 R>>endobj`);
 for(let i=0;i<3;i++){const c=page(i+1);add(`${6+i} 0 obj<</Length ${c.length}>>stream\n${c}\nendstream endobj`);}
 add('9 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj');
 const body=parts.join('\n');const xref=body.length+1;let x=`xref\n0 10\n0000000000 65535 f \n`;for(const o of off)x+=String(o).padStart(10,'0')+' 00000 n \n';
 return Buffer.from(body+'\n'+x+`trailer<</Size 10/Root 1 0 R>>\nstartxref\n${xref}\n%%EOF\n`);}
async function upload(page,pid,category,tag){const r=await page.request.post(`/api/patients/${pid}/documents`,{headers:{'X-Requested-With':'BabyCare'},multipart:{file:{name:`${tag}.pdf`,mimeType:'application/pdf',buffer:pdf3(tag)},category,title:`Synthetic ${category} ${tag}`}});expect(r.status(),await r.text()).toBe(201);return (await r.json()).id;}
async function openReview(page,id){await page.locator('nav [data-tab=documents]').click();await page.locator(`.doc-card[data-id="${id}"]`).first().click();await page.locator(`[data-action=review][data-id="${id}"]`).first().click();await expect(page.getByRole('dialog',{name:'Document extraction review'})).toBeVisible();}

test('review: precise errors with page jump, excluded rows never block, confirm creates history, transcription-only saves',async({page})=>{
 test.setTimeout(120000);const errors=[];page.on('pageerror',e=>errors.push(e.message));const shot=n=>page.screenshot({path:`.cache/screenshots/review-${n}.png`,fullPage:true});
 await onboard(page);const pid=(await (await page.request.get('/api/patients',{headers:{'X-Requested-With':'BabyCare'}})).json())[0].id;
 const tag='R'+Date.now();const doc=await upload(page,pid,'prescription',tag);
 await page.reload();await openReview(page,doc);
 const dlg=page.getByRole('dialog',{name:'Document extraction review'});
 // two manual rows: page 1 clean; page 3 with an unreadable dose and no name; plus an excluded incomplete vaccine suggestion
 await dlg.getByRole('button',{name:'Add medicine row'}).click();
 await dlg.locator('[name=r0_name]').fill('Synthetic syrup');await dlg.locator('[name=r0_dosage]').fill('2.5');await dlg.locator('[name=r0_dose_unit]').fill('ml');
 await dlg.getByRole('button',{name:'Add medicine row'}).click();
 await dlg.locator('[name=r1_page]').fill('3');await dlg.locator('[name=r1_dosage]').fill('2.?');
 await dlg.getByRole('button',{name:'Add vaccine row'}).click();
 await dlg.locator('[name=r2_included]').uncheck();   // excluded and incomplete: must not block
 await dlg.locator('[name=checked_against_original]').check();
 await dlg.getByRole('button',{name:'Confirm and save to history'}).click();
 const summary=dlg.locator('#review-errors');await expect(summary).toBeVisible();
 await expect(summary).toContainText('2 items need review before this document can be confirmed.');
 await expect(summary).toContainText('Page 3 · Medicine row 2: Name is required');
 await expect(summary).toContainText('Page 3 · Medicine row 2: Dose is marked unreadable');
 await expect(summary).not.toContainText('Vaccine');
 await expect(dlg.locator('[data-rvrow="1"]')).toHaveClass(/rv-bad/);
 await expect(dlg.locator('[data-rvfield="r1_dosage"]')).toHaveClass(/rv-bad/);
 await expect(dlg.locator('[data-rvfield="r1_dosage"] .rv-msg')).toContainText('Dose is marked unreadable');
 await expect(page.locator('#review-page')).toHaveValue('3');   // jumped to the page of the first problem
 await expect(page.locator('#original-page')).toHaveAttribute('data-page','3');   // and the original shown is page 3
 await expect(dlg.locator('[name=r0_name]')).toHaveValue('Synthetic syrup');   // edits preserved
 await shot('01-errors');
 // fix the row: correct name, leave the unreadable dose BLANK (never guessed), confirm
 await dlg.locator('[name=r1_name]').fill('Synthetic drops');await dlg.locator('[name=r1_dosage]').fill('');
 await dlg.getByRole('button',{name:'Confirm and save to history'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0,{timeout:15000});
 await expect(page.locator('.toast')).toContainText('Saved to history: 2 records');
 await page.locator('nav [data-tab=medicines]').click();
 await expect(page.locator('#app')).toContainText('Synthetic syrup');await expect(page.locator('#app')).toContainText('Synthetic drops');
 const meds=await (await page.request.get(`/api/patients/${pid}/medicines`,{headers:{'X-Requested-With':'BabyCare'}})).json();
 expect(meds.map(m=>[m.name,m.source_document_id,m.source_page,m.status,m.dosage]).sort()).toEqual([['Synthetic drops',doc,3,'draft',''],['Synthetic syrup',doc,1,'draft','2.5']]);
 await shot('02-history');
 // transcription-only document: no rows → "Save reviewed document"
 const bill=await upload(page,pid,'bill','B'+Date.now());await page.reload();await openReview(page,bill);
 const d2=page.getByRole('dialog',{name:'Document extraction review'});
 await expect(d2.getByRole('button',{name:'Save reviewed document'})).toBeVisible();
 await d2.locator('[name=checked_against_original]').check();await d2.getByRole('button',{name:'Save reviewed document'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0,{timeout:15000});await expect(page.locator('.toast')).toContainText('Reviewed document saved to the Document Library');
 const docs=await (await page.request.get(`/api/patients/${pid}/documents`,{headers:{'X-Requested-With':'BabyCare'}})).json();
 expect(docs.find(x=>x.id===bill).extraction_status).toBe('reviewed');expect(docs.find(x=>x.id===doc).extraction_status).toBe('reviewed');
 await shot('03-library');
 expect(errors).toEqual([]);
});
