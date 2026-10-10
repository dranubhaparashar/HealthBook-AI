const {test,expect}=require('@playwright/test');
const path=require('path');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`lifetime-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}
async function api(page,method,url,data){const r=await page.request.fetch(url,{method,data,headers:{'X-Requested-With':'BabyCare','Content-Type':'application/json'}});expect(r.ok(),`${method} ${url} ${await r.text()}`).toBeTruthy();return r.json();}

test('lifetime health page: tiles, evidence, calendar, parent entry and ask',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 const pid=(await api(page,'GET','/api/patients'))[0].id;
 // synthetic documents with bundle-style references
 const up=async(ref,category,date)=>{const r=await page.request.post(`/api/patients/${pid}/documents`,{headers:{'X-Requested-With':'BabyCare'},multipart:{file:{name:`${ref}.pdf`,mimeType:'application/pdf',buffer:Buffer.concat([require('fs').readFileSync(path.resolve('.cache/fixtures/vaccination.pdf')),Buffer.from('%'+ref)])},category,title:`Synthetic ${ref}`,source_ref:ref,...(date?{record_date:date}:{})}});expect(r.ok(),await r.text()).toBeTruthy();return (await r.json()).id;};
 const rx=await up('NEW_34','prescription','2026-08-19');const dis=await up('NEW_62','discharge','2026-10-02');
 await api(page,'POST',`/api/patients/${pid}/lifetime/fever`,{title:'Fictional fever Aug 2026',first_documented_on:'2026-08-19',last_documented_on:'2026-08-20',documented_fever_days:2,date_certainty:'exact',highest_temperature:'99.6',temperature_unit:'F',symptoms:'loose stools, vomiting',source_document_id:rx,evidence:[{source_document_id:rx,source_page:1,quoted_text:'fever today',evidence_kind:'fever_mention',observed_on:'2026-08-19'}]});
 await api(page,'POST',`/api/patients/${pid}/lifetime/antibiotics`,{generic_name:'Omnix-100',route:'oral',setting:'outpatient',administration_status:'prescribed_only',prescribed_on:'2026-08-19',prescribed_duration_days:5,source_document_id:rx,evidence:[{source_document_id:rx,source_page:1,quoted_text:'5 day syp Omnix-100 4 ml BD',evidence_kind:'prescription'}]});
 await api(page,'POST',`/api/patients/${pid}/lifetime/antibiotics`,{generic_name:'meropenem',route:'IV',setting:'inpatient',administration_status:'confirmed_administered',actual_start:'2026-09-28',actual_stop:'2026-10-01',source_document_id:dis});
 await page.locator('nav [data-tab=lifetime]').click();
 await expect(page.getByRole('heading',{name:'Lifetime Health'})).toBeVisible();
 await expect(page.locator('.lh-tile').first()).toBeVisible();
 const tile=page.locator('.lh-tile[data-key=distinct_antibiotics]');
 await expect(tile.locator('.lh-value')).toHaveText('2');
 await expect(page.locator('.lh-tile[data-key=exposure_days] .lh-value')).toHaveText('4');
 await expect(page.locator('.lh-tile[data-key=fever_episodes] .lh-value')).toHaveText('1');
 await tile.click();
 await expect(page.getByRole('dialog',{name:'Distinct antibiotics'})).toBeVisible();
 await expect(page.getByRole('dialog')).toContainText('cefixime');
 await page.getByRole('dialog').getByRole('button',{name:'Evidence'}).first().click();
 await expect(page.getByRole('dialog',{name:'cefixime · oral'})).toBeVisible();
 await expect(page.locator('#lh-page-img')).toBeVisible();
 await expect(page.getByRole('dialog')).toContainText('5 day syp Omnix-100 4 ml BD');
 await page.screenshot({path:'.cache/screenshots/lifetime-evidence.png',fullPage:true});
 await page.keyboard.press('Escape');
 await page.locator('.lh-tab[data-key=fever]').click();
 await expect(page.locator('.lh-day.fever').first()).toBeVisible();
 await page.locator('.lh-day.fever').first().click();
 await expect(page.getByRole('dialog')).toContainText('Fictional fever Aug 2026');
 await page.keyboard.press('Escape');
 await page.getByRole('button',{name:'✓ Verified against original'}).first().click();
 await page.locator('.lh-tab[data-key=antibiotics]').click();
 await expect(page.locator('.lh-tile[data-key=antibiotic_courses] .lh-value')).toHaveText('2');
 await expect(page.locator('svg[aria-label="Antibiotic courses over time"]')).toBeVisible();
 await page.screenshot({path:'.cache/screenshots/lifetime-antibiotics.png',fullPage:true});
 await page.locator('.lh-tab[data-key=parent]').click();
 await page.locator('#lh-parent-form [name=started_on]').fill('2026-04-10');
 await page.locator('#lh-parent-form [name=ended_on]').fill('2026-04-11');
 await page.locator('#lh-parent-form [name=temperature]').fill('101.2');
 await page.locator('#lh-parent-form [name=temperature_unit]').selectOption('F');
 await page.locator('#lh-parent-form [name=symptoms]').fill('home fever, runny nose');
 await page.getByRole('button',{name:'Save parent-reported entry'}).click();
 await expect(page.locator('.lh-badge.parent').first()).toBeVisible();
 await page.locator('.lh-tab[data-key=overview]').click();
 await expect(page.locator('.lh-tile[data-key=fever_episodes] .lh-value')).toHaveText('2');
 await page.screenshot({path:'.cache/screenshots/lifetime-overview.png',fullPage:true});
 await page.locator('nav [data-tab=search]').click();
 await page.locator('[name=q]').fill('How many different antibiotics has my child received?');
 await page.locator('#search-form').getByRole('button').click();
 await expect(page.locator('#search-results')).toContainText(/Distinct antibiotic active ingredients documented: 2|cefixime/);
 expect(errors).toEqual([]);
});

test('lifetime page is usable at phone width',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await onboard(page);
 await page.locator('#destination').selectOption('lifetime');
 await expect(page.getByRole('heading',{name:'Lifetime Health'})).toBeVisible();
 await expect(page.locator('.lh-tile').first()).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
 await page.screenshot({path:'.cache/screenshots/lifetime-mobile.png',fullPage:true});
});
