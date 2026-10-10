const {test,expect}=require('@playwright/test');
const fs=require('fs');const path=require('path');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`care-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}
function jpeg(){const b=Buffer.from('/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAAIAAgDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFQEBAQAAAAAAAAAAAAAAAAAAAAX/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIRAxEAPwCdABmX/9k=','base64');return b;}

test('daily care: quick-add, medicine GIVEN, stool photo observation, calendar and today card',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 // Today card and quick actions are on the dashboard
 await expect(page.locator('.dc-quick').first()).toBeVisible();
 await page.locator('.dc-quick [data-action=dc-add][data-kind=fluid]').first().click();
 await expect(page.getByRole('dialog',{name:'Add Water / fluid'})).toBeVisible();
 await page.locator('.dc-amounts button[data-ml="100"]').click();
 await page.getByRole('button',{name:'Save fluid entry'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 await page.locator('nav [data-tab=care]').click();
 await expect(page.getByRole('heading',{name:'Daily Health'})).toBeVisible();
 await expect(page.locator('.dc-stat.fluid .v')).toContainText('100');
 // diaper
 await page.locator('.dc-quick [data-action=dc-add][data-kind=diaper]').first().click();
 await page.locator('#dc-diaper-form label:has(input[value=wet])').click();
 await page.getByRole('button',{name:'Save diaper'}).click();
 await expect(page.locator('.dc-stat.diaper .v')).toHaveText('1');
 // medicine by name: scheduled items are never auto-given
 await page.locator('.dc-quick [data-action=dc-add][data-kind=medicine]').first().click();
 await page.locator('#dc-medicine-form [name=medicine_name]').fill('Zinc');
 await page.locator('#dc-medicine-form [name=dose]').fill('1');
 await page.locator('#dc-medicine-form [name=dose_unit]').fill('ml');
 await page.getByRole('button',{name:'Save',exact:true}).click();
 await expect(page.locator('.dc-stat.med .v')).toContainText('1');
 await expect(page.locator('.dc-stream')).toContainText('Zinc — given');
 // stool with photo -> image-based observation, never a diagnosis
 const tmp=path.resolve('.cache/fixtures/stool-test.jpg');fs.writeFileSync(tmp,jpeg());
 await page.locator('.dc-quick [data-action=dc-add][data-kind=stool]').first().click();
 await page.locator('#dc-intake-files').setInputFiles(tmp);
 // the photo becomes a review card: time must be confirmed (no EXIF, no date in the name), values are AI suggestions only
 await expect(page.getByRole('dialog',{name:'Review stool photos'})).toBeVisible();
 await expect(page.getByRole('heading',{name:'1 stool photo detected'})).toBeVisible();
 await expect(page.locator('.dc-intake-card .dc-detected')).toContainText(/Time not detected|weak/);
 const dialogText=await page.getByRole('dialog').innerText();
 expect(dialogText).toContain('Image-based observation only — not a diagnosis.');
 expect(dialogText.toLowerCase()).not.toMatch(/coli|infection|bacteri/);
 const when=new Date();const pad=n=>String(n).padStart(2,'0');
 await page.locator('.dc-intake-card input[name=at]').fill(`${when.getFullYear()}-${pad(when.getMonth()+1)}-${pad(when.getDate())}T00:30`);
 await page.locator('.dc-intake-card label:has(input[value=loose])').click();
 await page.getByRole('button',{name:'Confirm & save'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 // filters
 await page.locator('.dc-chip[data-filter=stool]').click();
 await expect(page.locator('.dc-stream .dc-ev')).toHaveCount(1);
 await page.locator('.dc-chip[data-filter=all]').click();
 await expect(page.locator('.dc-stream .dc-ev')).toHaveCount(4);   // the stool diaper logged with the photo is part of the stool item, not a second event
 // analytics + calendar
 await page.locator('.dc-tab[data-key=analytics]').click();
 await expect(page.locator('.dc-stat.fluid .v')).toContainText('100');
 await page.locator('nav [data-tab=calendar]').click();
 await expect(page.getByRole('heading',{name:'Care Calendar'})).toBeVisible();
 const today=await page.evaluate(()=>S.today.date);
 await expect(page.locator(`.dc-day[data-day="${today}"] .dots .dot`)).toHaveCount(3);
 await page.locator(`.dc-day[data-day="${today}"]`).click();
 await expect(page.getByRole('heading',{name:/Everything recorded on/})).toBeVisible();
 await page.getByRole('button',{name:'Week'}).click();
 await expect(page.locator('.dc-week .dc-pill').first()).toBeVisible();
 await page.screenshot({path:'.cache/screenshots/care-calendar-week.png',fullPage:true});
 // camera page: credentials never displayed
 await page.locator('nav [data-tab=camera]').click();
 await page.getByRole('button',{name:'＋ Add camera'}).click();
 await page.locator('#dc-camera-form [name=name]').fill('Nursery');
 await page.locator('#dc-camera-form [name=stream_url]').fill('rtsp://192.168.1.44:554/stream1');
 await page.locator('#dc-camera-form [name=username]').fill('admin');
 await page.locator('#dc-camera-form [name=password]').fill('supersecret');
 await page.getByRole('button',{name:'Add camera',exact:true}).click();
 await expect(page.locator('.dc-camcard')).toContainText('rtsp://192.168.1.44:554/stream1');
 expect(await page.locator('#app').innerText()).not.toContain('supersecret');
 await page.getByRole('button',{name:'Test connection'}).click();
 await expect(page.locator('.dc-camcard')).toContainText(/Test failed/);
 await page.screenshot({path:'.cache/screenshots/care-camera.png',fullPage:true});
 // Ask routes daily-care questions to the journal
 await page.locator('nav [data-tab=search]').click();
 await page.locator('[name=q]').fill('How much water did he drink today?');
 await page.locator('#search-form').getByRole('button').click();
 await expect(page.locator('#search-results')).toContainText('Fluids recorded today: 100 ml');
 expect(errors).toEqual([]);
});

test('daily care works one-handed at phone width',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await onboard(page);
 await page.locator('#destination').selectOption('care');
 await expect(page.getByRole('heading',{name:'Daily Health'})).toBeVisible();
 await expect(page.locator('.dc-quick button').first()).toBeVisible();
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth+1)).toBeTruthy();
 await page.screenshot({path:'.cache/screenshots/care-mobile.png',fullPage:true});
});

test('typing in an open dialog survives a background re-render',async({page})=>{
 await page.setViewportSize({width:390,height:844});
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`rerender-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:/all together\./})).toBeVisible();
 await page.locator('#destination').selectOption('growth');
 await page.getByRole('button',{name:'Add measurement'}).click();
 await page.locator('[name=weight_kg]').fill('10.2');
 await page.locator('[name=milestone]').fill('Typed before a late refresh');
 await page.evaluate(()=>render());   // what a late Daily Health load does
 await expect(page.locator('[name=weight_kg]')).toHaveValue('10.2');
 await expect(page.locator('[name=milestone]')).toHaveValue('Typed before a late refresh');
 await page.getByRole('button',{name:'Save record'}).click();
 await expect(page.getByText('Typed before a late refresh')).toBeVisible();
});
