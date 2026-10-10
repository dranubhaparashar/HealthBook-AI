const {test,expect}=require('@playwright/test');

test('HealthBook AI: login branding, adult profile with vitals, family member switch with age-aware modules',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('/');
 await expect(page).toHaveTitle(/HealthBook AI/);
 await expect(page.locator('.authcard .brand')).toHaveText('HealthBook AI');
 await expect(page.locator('.authcard')).toContainText('Your private family health workspace.');
 await expect(page.getByRole('heading',{name:/Every health detail,\s*one calm place\./})).toBeVisible();
 await expect(page.locator('.authcard')).toContainText('Keep reports, medicines, hospital visits, daily health and family records together.');
 expect(await page.locator('body').innerText()).not.toContain('BabyCare');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`family-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 // first profile: an adult for themselves
 await expect(page.getByRole('heading',{name:'Create the first health profile'})).toBeVisible();
 await page.locator('[name=name]').fill('Fictional Adult');
 await page.locator('[name=dob]').fill('1988-02-14');
 await page.locator('#child-form [name=relationship]').selectOption('self');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Adult’s health, all together.'})).toBeVisible();
 const adultId=await page.evaluate(()=>S.pid);
 await expect(page.locator('nav [data-tab=vitals]')).toBeVisible();
 await expect(page.locator('nav [data-tab=vaccines]')).toHaveCount(0);   // adults: vaccinations off by default
 await expect(page.locator('#pm-today-vitals')).toBeVisible();
 // vitals
 await page.locator('nav [data-tab=vitals]').click();
 await expect(page.getByRole('heading',{name:/vitals and daily health/})).toBeVisible();
 await page.locator('#pv-form [name=systolic]').fill('124');await page.locator('#pv-form [name=diastolic]').fill('79');await page.locator('#pv-form [name=heart_rate]').fill('68');await page.locator('#pv-form [name=spo2]').fill('98');
 await page.getByRole('button',{name:'Save reading'}).click();
 await expect(page.locator('#pv-body')).toContainText('124/79');
 await expect(page.locator('#pv-body')).toContainText('Blood pressure recorded on 1 of 14 days');
 const bodyText=(await page.locator('#pv-body').innerText()).toLowerCase();expect(bodyText).not.toMatch(/hypertension|diagnos/);
 // module settings per profile
 await page.locator('nav [data-tab=settings]').click();
 await expect(page.locator('#pm-modules')).toContainText('Adult profile');
 await page.locator('#pm-modules-form input[value=vaccines]').check();
 await page.getByRole('button',{name:'Save modules'}).click();
 await expect(page.locator('nav [data-tab=vaccines]')).toBeVisible();
 // add a family member (infant): separate data, age-aware navigation
 await page.getByRole('button',{name:'+ Add family member'}).click();
 await expect(page.getByRole('dialog',{name:'Add a family member'})).toBeVisible();
 await page.locator('#child-form [name=name]').fill('Fictional Infant');
 await page.locator('#child-form [name=dob]').fill('2026-06-01');
 await page.locator('#child-form [name=relationship]').selectOption('child');
 await page.locator('#child-form').getByRole('button').last().click();
 await expect(page.locator('#patient-select')).toContainText('Fictional Infant');
 await page.locator('nav [data-tab=today]').click();
 await expect(page.getByRole('heading',{name:'Fictional Infant’s health, all together.'})).toBeVisible();
 await expect(page.locator('nav [data-tab=vaccines]')).toBeVisible();
 await expect(page.locator('nav [data-tab=vitals]')).toHaveCount(0);
 // Ask is scoped: the infant has no vitals
 await page.locator('nav [data-tab=search]').click();
 await page.locator('[name=q]').fill('What was my blood pressure today?');
 await page.locator('#search-form').getByRole('button').click();
 await expect(page.locator('#search-results')).not.toContainText('124/79');
 // back to the adult: the reading is still there and only there
 await page.locator('#patient-select').selectOption(String(adultId));
 await page.locator('nav [data-tab=vitals]').click();
 await expect(page.locator('#pv-body')).toContainText('124/79');
 await page.screenshot({path:'.cache/screenshots/family-workspace.png',fullPage:true});
 expect(errors).toEqual([]);
});
