const {test,expect}=require('@playwright/test');
const path=require('path');const fs=require('fs');

async function onboard(page){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`intake-${Date.now()}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Test Child');
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Test Child’s health, all together.'})).toBeVisible();
}
const pad=n=>String(n).padStart(2,'0');
const iso=d=>`${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
const daysAgo=n=>{const d=new Date();d.setDate(d.getDate()-n);return d;};
const H={'X-Requested-With':'BabyCare'};

test('stool photos: batch upload, detected times (EXIF / WhatsApp name / none), AI suggestions, historical calendar placement, duplicates',async({page})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await onboard(page);
 const d2=iso(daysAgo(2)),d1=iso(daysAgo(1));
 const fx=path.resolve('.cache/fixtures');
 const files=[path.join(fx,'exif-stool.jpg'),path.join(fx,`WhatsApp Image ${d1} at 8.35.59 AM.jpg`),path.join(fx,'no-timestamp.jpg')];
 for(const f of files)expect(fs.existsSync(f),f).toBeTruthy();
 await page.locator('.dc-quick [data-action=dc-add][data-kind=stool]').first().click();
 await expect(page.getByRole('dialog',{name:'Add Stool + photo'})).toBeVisible();
 await page.locator('#dc-intake-files').setInputFiles(files);
 const dialog=page.getByRole('dialog',{name:'Review stool photos'});
 await expect(dialog).toBeVisible();
 await expect(page.getByRole('heading',{name:'3 stool photos detected'})).toBeVisible();
 const cards=page.locator('.dc-intake-card');
 await expect(cards).toHaveCount(3);
 // chronological: EXIF photo (2 days ago 14:05) first, WhatsApp (yesterday 8:35 AM) second, undated last
 await expect(cards.nth(0).locator('.dc-detected')).toContainText('2:05 PM');
 await expect(cards.nth(0).locator('.dc-detected')).toContainText('Source: EXIF (camera time)');
 await expect(cards.nth(1).locator('.dc-detected')).toContainText('8:35 AM');
 await expect(cards.nth(1).locator('.dc-detected')).toContainText('Source: file name');
 await expect(cards.nth(2).locator('.dc-detected')).toContainText(/Time not detected|file modified time \(weak\)/);await expect(cards.nth(2)).toHaveClass(/weak/);
 // per-day summary before anything is saved
 const summary=page.locator('.dc-intake-summary');
 await expect(summary).toContainText('These entries will be added to:');
 await expect(summary).toContainText('— 1');
 // AI suggestions are marked as suggestions and preselected
 await expect(cards.nth(0).locator('.dc-ai').first()).toContainText(/AI SUGGESTED · \d+%/);
 await expect(cards.nth(0).locator('input[name=colour]:checked')).toHaveValue('brown');
 const text=await dialog.innerText();
 expect(text).toContain('Image-based observation only — not a diagnosis.');
 expect(text.toLowerCase()).not.toMatch(/coli|infection|bacteri|parasite|virus/);
 await expect(page.locator('.dc-intake-thumb').first()).toHaveAttribute('src',/^blob:/);
 await page.screenshot({path:'.cache/screenshots/stool-intake-review.png',fullPage:true});
 // parent corrects the first card's colour, keeps the detected time, confirms
 await cards.nth(0).locator('label:has(input[value=green])').click();
 await cards.nth(0).getByRole('button',{name:'Confirm & save'}).click();
 await expect(cards.nth(0)).toHaveClass(/saved/);
 await expect(cards.nth(0)).toContainText('Saved');
 // the undated card shows the weak file-modified guess with an open, required time field; the parent corrects it
 await expect(cards.nth(2).locator('input[name=at]')).toBeVisible();
 await expect(cards.nth(2).locator('input[name=at]')).toHaveAttribute('required','');
 await cards.nth(2).locator('input[name=at]').fill(`${d2}T07:15`);
 await cards.nth(2).getByRole('button',{name:'Confirm & save'}).click();
 await expect(cards.nth(2)).toHaveClass(/saved/);
 // confirm the WhatsApp card as suggested; the dialog closes when nothing is pending
 await cards.nth(1).getByRole('button',{name:'Confirm & save'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 // Care Calendar day view: entries sit on their real days, not today
 await page.locator('nav [data-tab=calendar]').click();
 await page.locator('[data-action=dc-calview][data-key=month]').click();
 await page.locator(`[data-action=dc-open-day][data-day="${d2}"]`).first().click();
 await expect(page.getByRole('heading',{name:/Everything recorded on/})).toBeVisible();
 const dayText=await page.locator('#app').innerText();
 expect(dayText).toContain('14:05');expect(dayText).toContain('07:15');
 expect(dayText).toMatch(/Stool · green/);expect(dayText).toMatch(/diaper changed/);expect(dayText).not.toMatch(/Stool diaper/);
 await page.screenshot({path:'.cache/screenshots/stool-intake-day.png',fullPage:true});
 // a stool diaper was created with each entry (default on); suggestion and parent values are both kept
 const pid=(await (await page.request.fetch('/api/patients',{headers:H})).json())[0].id;
 const day=await (await page.request.fetch(`/api/patients/${pid}/care/day?day=${d2}`,{headers:H})).json();
 expect(day.stool.entries).toBe(2);expect(day.diapers.stool).toBe(2);expect(day.stool.with_photo).toBe(2);
 const entry=day.stool.items.find(x=>x.colour==='green');
 expect(entry.ai_suggestion.colour.value).toBe('brown');expect(entry.parent_values.colour).toBe('green');expect(entry.ai_review_status).toBe('parent_corrected');expect(entry.time_source).toBe('exif');
 // uploading the same WhatsApp photo again is flagged as already recorded, never saved twice
 await page.locator('nav [data-tab=care]').click();
 await page.locator('.dc-quick [data-action=dc-add][data-kind=stool]').first().click();
 await page.locator('#dc-intake-files').setInputFiles(files[1]);
 await expect(page.getByRole('heading',{name:'1 stool photo detected'})).toBeVisible();
 await expect(page.locator('.dc-intake-card.dup')).toContainText('Already recorded');
 await expect(page.locator('.dc-intake-card.dup')).toContainText('8:35 AM');
 await expect(page.getByRole('button',{name:'Confirm & save'})).toHaveCount(0);
 await page.screenshot({path:'.cache/screenshots/stool-intake-duplicate.png',fullPage:true});
 await page.getByRole('button',{name:'Discard'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 const stools=await (await page.request.fetch(`/api/patients/${pid}/care/stool?start=${d2}&end=${iso(new Date())}`,{headers:H})).json();
 expect(stools.length).toBe(3);
 // acceptance flow: one WhatsApp photo, no manual typing, Confirm & save -> appears at 20:52 on its day
 await page.locator('.dc-quick [data-action=dc-add][data-kind=stool]').first().click();
 await page.locator('#dc-intake-files').setInputFiles(path.join(fx,`WhatsApp Image ${d1} at 8.52.01 PM.jpg`));
 await expect(page.getByRole('heading',{name:'1 stool photo detected'})).toBeVisible();
 const card=page.locator('.dc-intake-card').first();
 await expect(card.locator('.dc-detected')).toContainText('8:52 PM');await expect(card.locator('.dc-detected')).toContainText('Source: file name');
 await expect(card.locator('input[name=colour]:checked')).not.toHaveValue('');
 await expect(card.locator('input[name=consistency]:checked')).not.toHaveValue('');
 await expect(card.locator('.dc-ai').first()).toContainText('AI SUGGESTED');
 await card.getByRole('button',{name:'Confirm & save'}).click();
 await expect(page.getByRole('dialog')).toHaveCount(0);
 // Daily Health journal of that day shows the combined item at 20:52 with its thumbnail
 await page.locator('nav [data-tab=care]').click();await page.locator('[data-action=dc-today]').first().click();await page.locator('[data-action=dc-prev]').first().click();
 await expect(page.locator('.dc-stream .dc-ev.stool').filter({hasText:'20:52'})).toHaveCount(1);
 await expect(page.locator('.dc-stream .dc-ev.stool').filter({hasText:'20:52'}).locator('img.dc-thumb')).toHaveAttribute('src',/^blob:/);
 // details: thumbnail, captured/uploaded/source, confirmed labels; still there after a reload
 await page.locator('.dc-stream .dc-ev.stool').filter({hasText:'20:52'}).getByRole('button',{name:'Photo & details'}).click();
 const detail=page.getByRole('dialog',{name:/Stool entry/});
 await expect(detail).toBeVisible();await expect(detail.locator('img.dc-photo')).toHaveAttribute('src',/^blob:/);
 await expect(detail).toContainText('Captured');await expect(detail).toContainText('20:52');await expect(detail).toContainText('Uploaded');await expect(detail).toContainText('Timestamp source');await expect(detail).toContainText('time from file name');
 await expect(detail).toContainText('CAREGIVER CONFIRMED');await expect(detail).not.toContainText('No photograph attached');await expect(detail).not.toContainText('not noted');
 await expect(detail.getByRole('button',{name:'View original'})).toBeVisible();await expect(detail.getByRole('button',{name:'Edit values'})).toBeVisible();await expect(detail.getByText('Replace photo')).toBeVisible();await expect(detail.getByRole('button',{name:'Remove photo'})).toBeVisible();
 await page.screenshot({path:'.cache/screenshots/stool-detail-confirmed.png',fullPage:true});
 await page.keyboard.press('Escape');
 await page.reload();await page.locator('nav [data-tab=care]').click();await page.locator('[data-action=dc-today]').first().click();await page.locator('[data-action=dc-prev]').first().click();
 await expect(page.locator('.dc-stream .dc-ev.stool').filter({hasText:'20:52'}).locator('img.dc-thumb')).toHaveAttribute('src',/^blob:/);
 // Care Calendar month marks that day with a stool dot
 await page.locator('nav [data-tab=calendar]').click();await page.locator('[data-action=dc-calview][data-key=month]').click();
 await expect(page.locator(`[data-action=dc-open-day][data-day="${d1}"] .dot.stool`)).toHaveCount(1);
 expect(errors).toEqual([]);
});
