// Request-lifecycle regression tests for Daily Care. Rendering must never start a request; one patient + one day = one request.
const {test,expect}=require('@playwright/test');

function tracker(page){
 const log=[];
 page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))log.push({method:r.method(),path:u.pathname,q:u.searchParams,at:Date.now()});});
 const since=(t,fn)=>log.filter(x=>x.at>=t&&fn(x));
 return {log,since,
  patient:(t)=>since(t,x=>/^\/api\/patients\/\d+\//.test(x.path)),
  careDay:(t,day)=>since(t,x=>x.method==='GET'&&/\/care\/day$/.test(x.path)&&(day===undefined||x.q.get('day')===day))};
}
async function onboard(page,name='Fictional Test Child'){
 await page.goto('/');
 await page.getByRole('button',{name:'New here? Create an account'}).click();
 await page.locator('[name=email]').fill(`storm-${Date.now()}-${Math.random().toString(36).slice(2,7)}@test.invalid`);
 await page.locator('[name=password]').fill('fictional-browser-password');
 await page.getByRole('button',{name:/Create private account/}).click();
 await page.locator('[name=name]').fill(name);
 await page.locator('[name=dob]').fill('2025-06-15');
 await page.getByRole('button',{name:/Create profile/}).click();
 await expect(page.getByRole('heading',{name:`${name}’s health, all together.`})).toBeVisible();
}
const settle=(page,ms)=>page.waitForTimeout(ms);

test('A: the login page makes zero patient requests over 5 seconds',async({page})=>{
 const t=tracker(page);const start=Date.now();
 await page.goto('/');await expect(page.locator('[name=email]')).toBeVisible();await settle(page,5000);
 expect(t.patient(start)).toEqual([]);
});

test('B+C: dashboard and Daily Care each load the day once and never loop',async({page})=>{
 const t=tracker(page);await onboard(page);
 const dash=Date.now();await settle(page,5000);
 const dashDay=t.careDay(dash-15000);
 expect(dashDay.length).toBe(1); // one Daily Care summary fetch for today, despite several module re-renders
 const today=dashDay[0].q.get('day');expect(today).toMatch(/^\d{4}-\d{2}-\d{2}$/);
 await page.locator('nav [data-tab=care]').click();await expect(page.getByRole('heading',{name:'Daily Health'})).toBeVisible();
 const care=Date.now();await settle(page,5000);
 expect(t.careDay(care).length).toBe(0); // today was already loaded for the dashboard; the Daily Care view reuses it
 await page.locator('nav [data-tab=calendar]').click();await settle(page,3000);
 expect(t.careDay(care).length).toBe(0);
});

test('D: adding water sends one POST and at most one refresh GET of the day',async({page})=>{
 const t=tracker(page);await onboard(page);await settle(page,1500);
 await page.locator('nav [data-tab=care]').click();await expect(page.getByRole('heading',{name:'Daily Health'})).toBeVisible();await settle(page,1000);
 const start=Date.now();
 await page.locator('.dc-quick [data-action=dc-add][data-kind=fluid]').first().click();
 await page.locator('.dc-amounts button[data-ml="100"]').click();
 await page.getByRole('button',{name:'Save fluid entry'}).click();
 await expect(page.locator('.dc-stat.fluid .v')).toContainText('100');await settle(page,5000);
 const posts=t.since(start,x=>x.method==='POST'&&/\/care\/fluid$/.test(x.path));
 expect(posts.length).toBe(1);
 expect(t.careDay(start).length).toBeLessThanOrEqual(1);
});

test('E+F: changing the day or the child triggers exactly one GET for the new target',async({page})=>{
 const t=tracker(page);await onboard(page,'Child One');await settle(page,1500);
 await page.locator('nav [data-tab=care]').click();await expect(page.getByRole('heading',{name:'Daily Health'})).toBeVisible();await settle(page,1000);
 const d=Date.now();await page.locator('[data-action=dc-prev]').first().click();await settle(page,4000);
 const prev=t.careDay(d);expect(prev.length).toBe(1);
 const y=new Date();y.setDate(y.getDate()-1);expect(prev[0].q.get('day')).toBeTruthy();
 // second child via the Add child modal, then switch back and forth once
 await page.getByRole('button',{name:'+ Add family member'}).click();
 await page.locator('#child-form [name=name]').fill('Child Two');await page.locator('#child-form [name=dob]').fill('2024-05-01');
 await page.locator('#child-form button.btn').click();
 await expect(page.locator('#patient-select')).toContainText('Child Two');await settle(page,4000);
 const sw=Date.now();
 const options=await page.locator('#patient-select option').allTextContents();const firstValue=await page.locator('#patient-select option').first().getAttribute('value');
 await page.locator('#patient-select').selectOption(firstValue);await settle(page,5000);
 const after=t.careDay(sw);
 expect(after.length).toBe(1);
 expect(after[0].path).toContain(`/patients/${firstValue}/`);
});

test('G: logout aborts Daily Care loading and nothing patient-specific runs afterwards',async({page})=>{
 const t=tracker(page);await onboard(page);await settle(page,1500);
 await page.locator('nav [data-tab=care]').click();await settle(page,1000);
 await page.getByRole('button',{name:'Sign out'}).click();await expect(page.locator('[name=email]')).toBeVisible();
 const out=Date.now();await settle(page,5000);
 expect(t.patient(out)).toEqual([]);
});

test('H: a 429 produces a quiet cooldown, not a retry storm',async({page})=>{
 const t=tracker(page);let injected=0;
 await page.route(/\/api\/patients\/\d+\/care\/day/,async route=>{if(route.request().url().includes('day=')&&injected<1){injected++;return route.fulfill({status:429,headers:{'Retry-After':'3','Content-Type':'application/json'},body:JSON.stringify({detail:'Too many requests. Try again shortly.'})});}return route.continue();});
 await onboard(page);
 const start=Date.now();await settle(page,1500);
 expect(injected).toBe(1);
 await expect(page.locator('text=/server is busy/i').first()).toBeVisible();
 expect(t.careDay(start).length).toBe(1); // no immediate retry
 await settle(page,5000); // past the 3 s Retry-After: exactly one deliberate retry, which succeeds
 const all=t.careDay(start);
 expect(all.length).toBe(2);
 await expect(page.locator('.dc-stats').first()).toBeVisible();
 await settle(page,3000);
 expect(t.careDay(start).length).toBe(2);
});
