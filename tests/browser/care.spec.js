const {test,expect}=require('@playwright/test');
const path=require('path');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`browser-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}

test('parent journey: upload card, correct extraction, save verified history and appointment',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 await page.getByRole('button',{name:'Scan vaccination card',exact:true}).first().click();
 await page.locator('input[type=file]').setInputFiles(path.resolve('.cache/fixtures/vaccination.pdf'));
 await page.getByRole('button',{name:'Securely upload'}).click();
 await expect(page.getByRole('dialog',{name:'Document extraction review'})).toBeVisible();
 await expect(page.locator('#original-page')).toBeVisible();
 await expect(page.locator('[name=r0_name]')).toHaveValue('MMR');
 await page.screenshot({path:'.cache/screenshots/vaccination-review.png',fullPage:true});
 await page.locator('[name=r0_dose_label]').fill('1');
 await page.locator('[name=r0_reminder_days]').fill('7,1,0');
 await page.locator('[name=r1_reminder_days]').fill('7,1,0');
 await page.locator('[name=r0_included]').check();
 await page.locator('[name=r1_included]').check();
 await page.locator('[name=r1_activate]').check();
 await page.locator('[name=checked_against_original]').check();
 await page.getByRole('button',{name:'Confirm and save to history'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 await page.locator('nav [data-tab=vaccines]').click();
 await expect(page.getByText('Verified administration',{exact:false}).first()).toBeVisible(); // status appears in the chart card and the table
 await expect(page.getByText('Confirmed appointment',{exact:false}).last()).toBeVisible();
 const dl=page.waitForEvent('download');await page.getByRole('button',{name:'Download vaccination chart'}).click();expect((await dl).suggestedFilename()).toBe('vaccination-chart.csv');
 await page.locator('nav [data-tab=search]').click();
 await page.locator('[name=q]').fill('Which vaccinations have been administered?');
 await page.locator('#search-form').getByRole('button').click();
 await expect(page.locator('#search-results')).toContainText('caregiver-verified administration date 2026-01-05');
 expect(errors).toEqual([]);
 await page.locator('nav [data-tab=today]').click();
 await page.screenshot({path:'.cache/screenshots/dashboard-desktop.png',fullPage:true});
});

test('responsive navigation exposes every section and persisted growth form',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await onboard(page);
 await page.locator('#destination').selectOption('growth');
 await page.getByRole('button',{name:'Add measurement'}).click();
 await page.locator('[name=record_date]').fill('2026-09-02');
 await page.locator('[name=weight_kg]').fill('10.2');
 await page.locator('[name=milestone]').fill('Fictional recorded milestone');
 await page.getByRole('button',{name:'Save record'}).click();
 await expect(page.getByText('Fictional recorded milestone')).toBeVisible();
 for(const tab of ['documents','medicines','hospital','vaccines','timeline','results','search','episodes','notifications','family','settings','today']){
   await page.locator('#destination').selectOption(tab);
   await expect(page.locator('h1').first()).toBeVisible();
 }
 await page.screenshot({path:'.cache/screenshots/dashboard-mobile.png',fullPage:true});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
});
