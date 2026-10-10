// Forgot password → email link → set new password → old password fails, new password works, data preserved.
// Needs a server started with BABYCARE_EMAIL_PROVIDER=dev-outbox and OUTBOX_FILE pointing to <data dir>/dev-outbox.jsonl.
const {test,expect}=require('@playwright/test');
const fs=require('fs');
const OUTBOX=process.env.OUTBOX_FILE;
function lastLinkFor(email){const lines=fs.readFileSync(OUTBOX,'utf-8').trim().split('\n').map(l=>JSON.parse(l)).filter(m=>m.to===email);const m=/#reset-password=([A-Za-z0-9_\-]+)/.exec(lines[lines.length-1].text);return m[1];}
test.skip(!OUTBOX,'needs OUTBOX_FILE');
test('forgot password on the web: generic message, reset page, sessions ended, new password works',async({page,context})=>{
 const errors=[];page.on('pageerror',e=>errors.push(e.message));const shot=n=>page.screenshot({path:`.cache/screenshots/pwreset-${n}.png`,fullPage:true});
 const email=`pwreset-${Date.now()}@test.invalid`,old='fictional-browser-password',fresh='a-brand-new-browser-password';
 await page.goto('/');await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(email);await page.locator('[name=password]').fill(old);await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill('Fictional Reset Child');await page.locator('[name=dob]').fill('2025-06-15');await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Reset Child’s health, all together.'})).toBeVisible();
 await page.getByRole('button',{name:'Sign out'}).click();
 // forgot password link on the login screen
 await page.getByRole('button',{name:'Forgot password?'}).click();
 await expect(page.getByRole('heading',{name:'Reset your password'})).toBeVisible();await shot('01-forgot');
 await page.locator('#pw-forgot-form [name=email]').fill(email);await page.getByRole('button',{name:'Email me a reset link'}).click();
 await expect(page.locator('#pw-sent')).toHaveText(/If an account exists for this email, a reset link has been sent/);
 // an unknown email gets the identical message
 await page.getByRole('button',{name:'Send again'}).click();await page.locator('#pw-forgot-form [name=email]').fill('nobody-'+Date.now()+'@test.invalid');await page.getByRole('button',{name:'Email me a reset link'}).click();
 await expect(page.locator('#pw-sent')).toHaveText(/If an account exists for this email, a reset link has been sent/);await shot('02-sent');
 // open the emailed link: token is removed from the address bar
 const token=lastLinkFor(email);await page.goto('/#reset-password='+token);
 await expect(page.getByRole('heading',{name:'Choose a new password'})).toBeVisible();expect(page.url()).not.toContain(token);
 await page.locator('#pw-reset-form [name=password]').fill(fresh);await page.locator('#pw-reset-form [name=password2]').fill(fresh+'x');await page.getByRole('button',{name:'Set new password'}).click();
 await expect(page.locator('#pw-error')).toHaveText('The two passwords do not match.');
 await page.locator('#pw-reset-form [name=password]').fill(fresh);await page.locator('#pw-reset-form [name=password2]').fill(fresh);await page.getByRole('button',{name:'Set new password'}).click();
 await expect(page.getByRole('heading',{name:'You can sign in now'})).toBeVisible();await shot('03-done');
 // the link cannot be used twice
 await page.goto('/#reset-password='+token);await expect(page.getByRole('heading',{name:'This link no longer works'})).toBeVisible();
 await page.getByRole('button',{name:'Back to sign in'}).click();
 // old password fails, new one works, profile is still there
 await page.locator('[name=email]').fill(email);await page.locator('[name=password]').fill(old);await page.getByRole('button',{name:/Sign in/}).click();
 await expect(page.locator('.toast')).toContainText('Incorrect email or password');
 await page.locator('[name=password]').fill(fresh);await page.getByRole('button',{name:/Sign in/}).click();
 await expect(page.getByRole('heading',{name:'Fictional Reset Child’s health, all together.'})).toBeVisible();
 expect(errors).toEqual([]);
});
