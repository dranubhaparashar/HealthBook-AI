// Offline shell only. Sensitive APIs and patient records are never cached.
const CACHE='babycare-shell-v16';
self.addEventListener('install',e=>e.waitUntil(caches.open(CACHE).then(c=>c.addAll(['/','/styles.css','/config.js','/app.js','/features.js','/history.js','/lifetime.js','/lifetime.css','/charts.js','/analytics.js','/analytics.css','/care.js','/care.css','/share.js','/share.css','/exports.js','/exports.css','/profiles.js','/password.js','/icon.svg']))));
self.addEventListener('activate',e=>e.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(k=>k!==CACHE).map(k=>caches.delete(k))))));
self.addEventListener('fetch',e=>{
 if(e.request.method!=='GET'||new URL(e.request.url).pathname.startsWith('/api/')) return;
 e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});
// Web Push: the payload is always the same generic sentence; no record detail is ever shown on the lock screen.
self.addEventListener('push',e=>{let data={};try{data=e.data?e.data.json():{};}catch{}e.waitUntil(self.registration.showNotification(data.title||'HealthBook AI',{body:data.body||'You have a confirmed care reminder. Open HealthBook AI to review it.',icon:'/icon.svg',tag:'babycare-reminder',renotify:true,data:{notification_id:data.notification_id||null}}));});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil(clients.matchAll({type:'window',includeUncontrolled:true}).then(list=>{for(const c of list){if('focus' in c)return c.focus();}return clients.openWindow('/');}));});
