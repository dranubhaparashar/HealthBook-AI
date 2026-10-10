import {Platform} from 'react-native';
import Constants from 'expo-constants';

export type Env = 'local' | 'staging' | 'production';
const extra: any = (Constants.expoConfig as any)?.extra || {};
export const ENV: Env = (process.env.EXPO_PUBLIC_ENV as Env) || extra.env || 'local';
const fromExtra: string = extra.apiUrl || '';
const fromEnv: string = process.env.EXPO_PUBLIC_API_URL || '';
const localDefault = Platform.OS === 'android' ? 'http://10.0.2.2:8000' : 'http://localhost:8000';
// Production/staging never fall back to localhost; a missing URL is reported in More → Deployment info.
export const SERVER = (fromEnv || fromExtra || (ENV === 'local' ? localDefault : '')).replace(/\/$/, '');
// Where doctor.html is served: the Firebase site in production, the API host locally.
export const WEB_ORIGIN = (process.env.EXPO_PUBLIC_WEB_ORIGIN || extra.webOrigin || SERVER).replace(/\/$/, '');
export const APP_VERSION: string = (Constants.expoConfig as any)?.version || '1.1.0';
export const API_CONFIGURED = SERVER.length > 0;
export const isSecureTransport = SERVER.startsWith('https://') || /^(http:\/\/)?(10\.0\.2\.2|localhost|127\.0\.0\.1)/.test(SERVER);
