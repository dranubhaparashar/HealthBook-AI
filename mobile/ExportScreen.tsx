import React, {useEffect, useState} from 'react';
import {View, Text, TouchableOpacity, TextInput} from 'react-native';
import * as FileSystem from 'expo-file-system/legacy';
import * as Sharing from 'expo-sharing';
import {Screen, Title, Card, H3, Btn, Row, Note, Segments, Check, Loading, Chip, Facts, Fact, fmtDate, msg, s, C} from './src/ui';

type API = (path: string, method?: string, body?: any, upload?: boolean) => Promise<any>;
type Preset = 'complete' | 'ecoli' | 'latest_hospitalization' | 'antibiotics' | 'labs' | 'vaccination' | 'daily_care_7d' | 'custom';
type Mode = 'summary' | 'with_sources' | 'archive';
const TILES: [Preset, string, string][] = [
  ['complete', 'Complete Medical Record', 'Everything, with every original report appended.'],
  ['ecoli', 'E. coli History', 'Cultures naming E. coli with the linked illness, hospitalization, prescriptions and labs.'],
  ['latest_hospitalization', 'Latest Hospitalization', 'The most recent admission with discharge summary and in-stay records.'],
  ['antibiotics', 'Antibiotic History', 'Every documented course and prescription with administration status.'],
  ['labs', 'Laboratory History', 'All lab and culture reports with values, trends and reference intervals.'],
  ['vaccination', 'Vaccination Record', 'Card entries with status; a due date is never shown as given.'],
  ['daily_care_7d', 'Daily Care · last 7 days', 'What you recorded: fluids, diapers, stool, medicines given, symptoms, sleep.'],
  ['custom', 'Custom', 'Choose sections and a date range.'],
];
const SECTIONS: [string, string][] = [['summary', 'Executive summary'], ['timeline', 'Medical timeline'], ['hospitalizations', 'Hospitalizations'], ['illnesses', 'Illness episodes'], ['prescriptions', 'Prescriptions'], ['medicines', 'Medicines'], ['antibiotics', 'Antibiotic history'], ['labs', 'Laboratory results'], ['cultures', 'Cultures'], ['vaccinations', 'Vaccinations'], ['growth', 'Growth'], ['daily_care', 'Daily Care journal'], ['stool_photos', 'Stool photographs (only if ticked)'], ['sources', 'Original source reports (appendix)']];
const MODES: [Mode, string][] = [['summary', 'PDF summary'], ['with_sources', 'PDF + originals'], ['archive', 'ZIP archive']];

export function ExportScreen({pid, api, server, token, initialPreset = 'complete'}: {pid: number; api: API; server: string; token: string; initialPreset?: Preset}) {
  const [preset, setPreset] = useState<Preset>(initialPreset);
  const [mode, setMode] = useState<Mode>('with_sources');
  const [sections, setSections] = useState<Set<string>>(new Set(SECTIONS.map(x => x[0]).filter(k => k !== 'stool_photos')));
  const [photos, setPhotos] = useState(false);
  const [from, setFrom] = useState(''); const [to, setTo] = useState('');
  const [preview, setPreview] = useState<any>(null); const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false); const [err, setErr] = useState(''); const [done, setDone] = useState<{name: string; uri: string; size: number} | null>(null);
  const spec = () => {
    const sp: any = {preset, mode, include_photos: preset === 'daily_care_7d' ? photos : sections.has('stool_photos')};
    if (preset === 'custom') {sp.sections = [...sections].filter(k => k !== 'stool_photos'); sp.include_sources = sections.has('sources'); if (from) sp.date_from = from; if (to) sp.date_to = to; if (sections.has('daily_care')) sp.daily_care_days = 7;}
    if (mode === 'summary') sp.include_sources = false;
    return sp;
  };
  const key = JSON.stringify(spec());
  useEffect(() => {let alive = true; setLoading(true); setErr(''); setDone(null);
    api(`/patients/${pid}/export/preview`, 'POST', spec()).then(pv => {if (alive) setPreview(pv);}).catch(e => {if (alive) {setErr(msg(e)); setPreview(null);}}).finally(() => {if (alive) setLoading(false);});
    return () => {alive = false;};
  }, [pid, key]);
  const generate = async () => {
    if (busy || !preview) return; setBusy(true); setErr(''); setDone(null);
    try {
      const sp = spec(); const q = new URLSearchParams({preset, mode, include_photos: String(!!sp.include_photos)});
      if (preset === 'custom') {q.set('sections', (sp.sections || []).join(',')); q.set('include_sources', String(!!sp.include_sources)); if (from) q.set('date_from', from); if (to) q.set('date_to', to); if (sp.daily_care_days) q.set('daily_care_days', String(sp.daily_care_days));}
      const name = preview.filename as string; const target = `${FileSystem.cacheDirectory}${name}`;
      const res = await FileSystem.downloadAsync(`${server}/api/patients/${pid}/export/download?${q.toString()}`, target, {headers: {Authorization: `Bearer ${token}`}});
      if (res.status !== 200) throw new Error(`The server answered ${res.status}. Sign in again and retry.`);
      const info = await FileSystem.getInfoAsync(res.uri); const size = (info as any).size || 0;
      setDone({name, uri: res.uri, size});
      if (await Sharing.isAvailableAsync()) await Sharing.shareAsync(res.uri, {mimeType: mode === 'archive' ? 'application/zip' : 'application/pdf', dialogTitle: `Share ${name}`, UTI: mode === 'archive' ? 'public.zip-archive' : 'com.adobe.pdf'});
    } catch (e) {setErr(msg(e));}
    setBusy(false);
  };
  const removeLocal = async () => {if (done) {try {await FileSystem.deleteAsync(done.uri, {idempotent: true});} catch {} setDone(null);}};
  const toggle = (k: string) => setSections(prev => {const n = new Set(prev); if (n.has(k)) n.delete(k); else n.add(k); return n;});
  const c = preview?.counts || {}; const r = preview?.date_range || {};
  return <Screen>
    <Title kicker="DOWNLOAD / SHARE RECORD" title="Medical record as a PDF" sub="A designed dossier with the original reports appended and clickable source references. Generated on the server for this request only."/>
    <View style={{gap: 8}}>{TILES.map(([k, t, d]) => <TouchableOpacity key={k} onPress={() => setPreset(k)} style={[s.card, {borderColor: preset === k ? C.s1 : C.line, borderWidth: preset === k ? 2 : 1, padding: 14}]}><Text style={{fontWeight: '700', color: C.ink}}>{t}</Text><Text style={s.subSmall}>{d}</Text></TouchableOpacity>)}</View>
    {preset === 'custom' && <Card><H3 sub="Leave dates empty for the whole record.">What to include</H3>{SECTIONS.map(([k, t]) => <Check key={k} label={t} value={sections.has(k)} onChange={() => toggle(k)}/>)}
      <Row><View style={{flex: 1}}><Text style={s.kicker}>FROM (YYYY-MM-DD)</Text><TextInput value={from} onChangeText={setFrom} placeholder="2026-01-01" style={s.input}/></View><View style={{flex: 1}}><Text style={s.kicker}>TO</Text><TextInput value={to} onChangeText={setTo} placeholder="2026-12-31" style={s.input}/></View></Row></Card>}
    {preset === 'daily_care_7d' && <Card><Check label="Include stool photographs" sub="Off by default; only when you tick this." value={photos} onChange={() => setPhotos(!photos)}/></Card>}
    <Card><H3>Output</H3><Segments items={MODES} value={mode} onChange={k => setMode(k as Mode)}/><Text style={[s.subSmall, {marginTop: 8}]}>{mode === 'summary' ? 'Designed summary only.' : mode === 'with_sources' ? 'Recommended for doctors: summary plus every original document as an appendix.' : 'Medical_Record.pdf, Original_Reports/ as separate files and manifest.csv.'}</Text></Card>
    <Card>
      {loading ? <Loading text="Preparing preview…"/> : err ? <Note tone="caution">{err}</Note> : preview ? <>
        <H3 sub={preview.filename}>{preview.title}</H3>
        <Facts><Fact k="Range" v={`${fmtDate(r.from)} to ${fmtDate(r.to)}${r.derived ? ' (documented events)' : ''}`}/><Fact k="Sections" v={(preview.sections || []).map((x: string) => x.replace('_', ' ')).join(' · ')}/><Fact k="Estimated pages" v={`≈ ${preview.estimated_pages}`}/></Facts>
        <Row style={{marginTop: 10}}><Chip label={`${c.sources ?? 0} source reports`} tone="brand"/><Chip label={`${c.prescriptions ?? 0} prescriptions`}/><Chip label={`${c.lab_documents ?? 0} lab reports`}/><Chip label={`${c.hospitalizations ?? 0} hospitalizations`}/><Chip label={`${c.antibiotic_courses ?? 0} antibiotic courses`}/>{c.pending ? <Chip label={`${c.pending} pending verification`} tone="warn"/> : null}</Row>
        <View style={{height: 12}}/>
        <Btn title={busy ? 'Generating…' : `Generate & share ${MODES.find(m => m[0] === mode)![1]}`} onPress={generate} disabled={busy}/>
        {done && <View style={{marginTop: 10, gap: 8}}><Note>{done.name} ({(done.size / 1048576).toFixed(1)} MB) is saved in this app's cache and was handed to the share sheet. It contains no account tokens.</Note><Btn title="Remove the copy from this phone" kind="alt" small onPress={removeLocal}/></View>}
      </> : null}
    </Card>
    <Note>The PDF never turns a prescription into an administration record, never counts culture susceptibility antibiotics as medicines, and never shows a vaccination due date as given. Original pages are appended unchanged.</Note>
  </Screen>;
}
