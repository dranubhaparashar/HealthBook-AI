// Environment-aware Expo config. Select with EXPO_PUBLIC_ENV=local|staging|production (default local).
// API URLs come from environment variables so no production build ever carries a hardcoded localhost.
const base = require('./app.json').expo;
const env = process.env.EXPO_PUBLIC_ENV || 'local';
// Public production endpoints (not secrets): the free Render API and the Firebase Hosting site.
const PRODUCTION_API = 'https://your-api.onrender.com';
const PRODUCTION_WEB = 'https://example-healthbook.web.app';
const apiByEnv = {
  local: process.env.EXPO_PUBLIC_API_URL_LOCAL || process.env.EXPO_PUBLIC_API_URL || 'http://10.0.2.2:8000',
  staging: process.env.EXPO_PUBLIC_API_URL_STAGING || process.env.EXPO_PUBLIC_API_URL || '',
  production: process.env.EXPO_PUBLIC_API_URL_PRODUCTION || process.env.EXPO_PUBLIC_API_URL || PRODUCTION_API,
};
const webByEnv = { local: '', staging: process.env.EXPO_PUBLIC_WEB_ORIGIN || '', production: process.env.EXPO_PUBLIC_WEB_ORIGIN || PRODUCTION_WEB };
if (env !== 'local' && /localhost|127\.0\.0\.1|10\.0\.2\.2|^http:/.test(apiByEnv[env] || '')) throw new Error(`The ${env} build must use an https API URL, never a local one`);
module.exports = {
  expo: {
    ...base,
    name: env === 'production' ? 'HealthBook AI' : `HealthBook AI ${env}`,
    version: '1.1.0',
    ios: { ...base.ios, bundleIdentifier: 'in.babycare.family' },
    android: { ...base.android, package: 'in.babycare.family', versionCode: 2, adaptiveIcon: undefined },
    extra: { ...(base.extra || {}), env, apiUrl: apiByEnv[env] || '', webOrigin: webByEnv[env] || process.env.EXPO_PUBLIC_WEB_ORIGIN || '', eas: base.extra?.eas },
    // Plain HTTP is allowed only for the local environment (emulator -> your PC). Staging/production are HTTPS-only.
    plugins: [...base.plugins, ['expo-build-properties', { android: { usesCleartextTraffic: env === 'local' } }]],
  },
};
