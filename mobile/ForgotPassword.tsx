import React, {useState} from 'react';
import {View, Text, TextInput, TouchableOpacity, ActivityIndicator, Linking} from 'react-native';
import {C, s as ui} from './src/ui';

/* Forgot password on the phone: the app asks the server to email a reset link (same generic answer whether or not the
   address is registered). The link opens the HealthBook AI website, where the new password is set; the app then signs
   in with it. Nothing about the account or the token is stored on the phone. */
export function ForgotPassword({server, webOrigin, initialEmail = '', onBack}: {server: string; webOrigin: string; initialEmail?: string; onBack: () => void}) {
  const [email, setEmail] = useState(initialEmail);
  const [busy, setBusy] = useState(false);
  const [sent, setSent] = useState('');
  const [error, setError] = useState('');
  const submit = async () => {
    const value = email.trim().toLowerCase();
    if (!value.includes('@')) {setError('Enter the email address you use for HealthBook AI.'); return;}
    setBusy(true); setError('');
    try {
      const res = await fetch(`${server}/api/auth/password-reset/request`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({email: value})});
      let body: any = {}; try {body = await res.json();} catch {}
      if (!res.ok) throw new Error(typeof body.detail === 'string' ? body.detail : 'The request could not be sent. Try again in a minute.');
      setSent(body.message || 'If an account exists for this email, a reset link has been sent.');
    } catch (e) {setError(e instanceof Error ? e.message : String(e));}
    setBusy(false);
  };
  const button = (label: string, onPress: () => void, alt = false) => <TouchableOpacity accessibilityRole="button" onPress={onPress} disabled={busy} style={[ui.btn, alt && ui.btnAlt, {marginTop: 10}]}><Text style={[ui.btnText, alt && {color: C.s1}]}>{label}</Text></TouchableOpacity>;
  return <View style={{gap: 6}}>
    <Text style={ui.kicker}>FORGOT PASSWORD</Text>
    <Text style={{fontSize: 24, fontWeight: '700', color: C.ink}}>{sent ? 'Check your email' : 'Reset your password'}</Text>
    {sent ? <>
      <Text style={ui.subSmall} accessibilityLiveRegion="polite">{sent}</Text>
      <Text style={ui.subSmall}>Open the link on this phone or any computer, choose a new password, then sign in here with it. Only the newest link works; it expires after 30 minutes.</Text>
      {webOrigin ? button('Open HealthBook AI website', () => Linking.openURL(webOrigin), true) : null}
      {button('Back to sign in', onBack)}
      {button('Send again', () => setSent(''), true)}
    </> : <>
      <Text style={ui.subSmall}>Enter the email address of your account. If it belongs to an account, we email a link to set a new password. Your profiles and records are not changed.</Text>
      <TextInput style={[ui.input, {marginTop: 10}]} autoCapitalize="none" autoComplete="email" keyboardType="email-address" value={email} onChangeText={setEmail} placeholder="Email" placeholderTextColor="#9aa7a3" accessibilityLabel="Email"/>
      {error ? <Text style={{color: '#9a3b36', marginTop: 6}}>{error}</Text> : null}
      {busy ? <ActivityIndicator color={C.s1} style={{marginTop: 12}}/> : button('Email me a reset link', submit)}
      {button('Back to sign in', onBack, true)}
    </>}
  </View>;
}
