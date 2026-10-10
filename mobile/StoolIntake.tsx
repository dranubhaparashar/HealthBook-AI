/* Intelligent stool-photo intake on the phone: pick one or many photos (camera or gallery), let the server read when each
   was really taken (EXIF, WhatsApp file name, metadata) and what it visibly shows, then confirm one review card per photo.
   Nothing is saved until the parent taps "Confirm & save"; AI values are suggestions and the parent's choices are kept
   alongside them. Image-based observation only — never a diagnosis. */
import React, {useState} from 'react';
import {View, Text, Image, Alert, TextInput, TouchableOpacity} from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import * as DocumentPicker from 'expo-document-picker';
import {Card, H3, Btn, Row, Note, Segments, Check, Chip, s, C} from './src/ui';

type API = (path: string, method?: string, body?: any, upload?: boolean) => Promise<any>;
export type IntakeCard = {id: number; status: string; original_filename: string; image_url: string; detected: {at: string | null; date: string | null; label: string; source: string; source_label: string; confidence: string; needs_confirmation: boolean; time_unknown: boolean; camera: string}; suggestions: any; prefill: any; duplicate_of: {entry_id: number; at: string; label: string} | null; saved_label?: string};
export type IntakePayload = {count: number; pending: number; duplicates: number; items: IntakeCard[]; by_date: {date: string; count: number}[]; disclaimer: string};

const COLOURS: [string, string][] = [['', 'Not noted'], ['brown', 'Brown'], ['yellow', 'Yellow'], ['green', 'Green'], ['dark_green', 'Dark green'], ['black', 'Black'], ['red', 'Red'], ['pale', 'Pale'], ['white', 'White'], ['other', 'Other / uncertain']];
const CONSISTENCY: [string, string][] = [['', 'Unable to determine'], ['watery', 'Watery'], ['loose', 'Loose'], ['soft', 'Soft'], ['formed', 'Formed'], ['hard', 'Hard']];
const AMOUNT: [string, string][] = [['', 'Not determined'], ['small', 'Small'], ['medium', 'Medium'], ['large', 'Large']];
const TRI: [string, string][] = [['uncertain', 'Uncertain'], ['yes', 'Yes'], ['no', 'No']];
const CONCERN: [string, string][] = [['none', 'None'], ['mild', 'Mild'], ['moderate', 'Moderate'], ['high', 'High']];

export async function pickStoolPhotos(camera: boolean): Promise<ImagePicker.ImagePickerAsset[] | null> {
  const perm = camera ? await ImagePicker.requestCameraPermissionsAsync() : await ImagePicker.requestMediaLibraryPermissionsAsync();
  if (perm.status !== 'granted') {Alert.alert('Permission needed', 'Photo permission was not granted.'); return null;}
  // quality 1 + exif keeps the original bytes (and their EXIF capture time) wherever the platform allows it
  const r = camera ? await ImagePicker.launchCameraAsync({mediaTypes: ['images'], quality: 1, exif: true})
                   : await ImagePicker.launchImageLibraryAsync({mediaTypes: ['images'], quality: 1, exif: true, allowsMultipleSelection: true, selectionLimit: 24, orderedSelection: true, legacy: true});  // legacy gallery keeps real file names (WhatsApp Image … .jpg); the new Android photo picker renames files to ids
  if (r.canceled || !r.assets?.length) return null;
  return r.assets;
}

export type PickedPhoto = {uri: string; name: string; mimeType: string; exifDate?: string; lastModified?: number};

export function fromImagePicker(assets: ImagePicker.ImagePickerAsset[]): PickedPhoto[] {
  return assets.map((a, i) => {const exif: any = a.exif || {}; return {uri: a.uri, name: a.fileName || `photo-${i + 1}.jpg`, mimeType: a.mimeType || 'image/jpeg', exifDate: String(exif.DateTimeOriginal || exif.DateTimeDigitized || exif.DateTime || '')};});
}

/* "Choose from files" keeps the real file name (WhatsApp Image 2026-10-06 at 8.35.59 AM.jpg, IMG-20261006-WA0012.jpg),
   which the Android photo picker replaces with a numeric id. The bytes (and any EXIF) are copied unchanged. */
export async function pickStoolFiles(): Promise<PickedPhoto[] | null> {
  const r = await DocumentPicker.getDocumentAsync({type: 'image/*', multiple: true, copyToCacheDirectory: true});
  if (r.canceled || !r.assets?.length) return null;
  return r.assets.map(a => ({uri: a.uri, name: a.name, mimeType: a.mimeType || 'image/jpeg', lastModified: a.lastModified}));
}

export const INTAKE_STAGES = ['Uploading photo…', 'Reading photo time…', 'Analysing visible characteristics…', 'Ready for review'];
export async function uploadStoolPhotos(api: API, pid: number, photos: PickedPhoto[]): Promise<IntakePayload> {
  const fd = new FormData();
  photos.forEach(a => {
    fd.append('files', {uri: a.uri, name: a.name, type: a.mimeType} as any);
    fd.append('client_names', a.name);
    fd.append('client_exif', a.exifDate || '');
    fd.append('file_modified', a.lastModified ? new Date(a.lastModified).toISOString() : '');
  });
  return api(`/patients/${pid}/care/stool/intake`, 'POST', fd, true);
}

type Draft = {at: string; colour: string; consistency: string; amount: string; mucus: string; blood: string; watery: boolean; diaper: boolean; concern: string; symptoms: string; note: string};
const tri = (v: boolean | null | undefined) => v === true ? 'yes' : v === false ? 'no' : 'uncertain';
const draftFor = (c: IntakeCard): Draft => ({at: c.detected.at ? c.detected.at.slice(0, 16) : '', colour: c.prefill?.colour || '', consistency: c.prefill?.consistency || '', amount: c.prefill?.amount || '', mucus: tri(c.prefill?.mucus_observed), blood: tri(c.prefill?.blood_observed), watery: !!c.prefill?.watery, diaper: true, concern: 'none', symptoms: '', note: ''});
const fmtDay = (d: string) => new Date(d + 'T12:00:00').toLocaleDateString('en-IN', {day: '2-digit', month: 'short', year: 'numeric'});

function Ai({sug, k}: {sug: any; k: string}) {
  const v = sug && sug[k]; if (!v || v.value == null) return null;
  const hidden = ['unable_to_determine', 'not_determined'].includes(v.value);
  return <Text style={{fontSize: 11, fontWeight: '700', color: C.s1, marginLeft: 6}}>{hidden ? 'AI: not determined' : `AI SUGGESTED${v.confidence_pct ? ` · ${v.confidence_pct}%` : v.confidence && v.confidence !== 'none' ? ` · ${v.confidence}` : ''}`}</Text>;
}

export function StoolIntakeReview({pid, api, server, token, intake, onChange, onDone, onCancel, onAddMore, onOpenEntry}: {pid: number; api: API; server: string; token: string; intake: IntakePayload; onChange: (p: IntakePayload) => void; onDone: () => void; onCancel: () => void; onAddMore: () => void; onOpenEntry?: (entryId: number, day: string) => void}) {
  const [drafts, setDrafts] = useState<Record<number, Draft>>(() => Object.fromEntries(intake.items.map(c => [c.id, draftFor(c)])));
  const [busy, setBusy] = useState<number | 'all' | null>(null);
  const [editTime, setEditTime] = useState<Record<number, boolean>>({});
  const pending = intake.items.filter(c => c.status === 'pending');
  const draft = (c: IntakeCard) => drafts[c.id] || draftFor(c);
  const set = (id: number, patch: Partial<Draft>) => setDrafts(d => ({...d, [id]: {...(d[id] || draftFor(intake.items.find(c => c.id === id)!)), ...patch}}));
  const byDate: Record<string, number> = {};
  for (const c of pending) {const k = c.detected.date || 'unknown'; byDate[k] = (byDate[k] || 0) + 1;}

  const confirm = async (c: IntakeCard) => {
    const d = draft(c); const t = (v: string) => v === 'yes' ? true : v === 'no' ? false : null;
    if (c.detected.needs_confirmation && !d.at) throw new Error('Enter when this photo was taken (YYYY-MM-DDTHH:MM).');
    const body: any = {colour: d.colour, consistency: d.consistency, amount: d.amount, mucus_observed: t(d.mucus), blood_observed: t(d.blood), watery: d.watery, create_diaper: d.diaper, parent_concern: d.concern, symptoms: d.symptoms, note: d.note, client_key: Math.random().toString(36).slice(2) + Date.now().toString(36)};
    if (d.at && (c.detected.needs_confirmation || !c.detected.at || d.at !== c.detected.at.slice(0, 16))) body.at = d.at;
    const saved = await api(`/care/stool-intake/${c.id}/confirm`, 'POST', body);
    onChange({...intake, items: intake.items.map(x => x.id === c.id ? {...x, status: 'saved', saved_label: `${fmtDay(saved.local_date)} · ${String(saved.at).slice(11, 16)}`} : x)});
    return saved;
  };
  const confirmOne = async (c: IntakeCard) => {setBusy(c.id); try {const saved = await confirm(c); if (saved.review_message) Alert.alert('Saved', `${saved.review_message} — image-based observation only, not a diagnosis.`);} catch (e: any) {Alert.alert('Not saved', e?.message || String(e));} finally {setBusy(null);}};
  const confirmAll = async () => {setBusy('all'); let ok = 0, bad = 0; for (const c of pending) {try {await confirm(c); ok++;} catch (e: any) {bad++;}} setBusy(null); Alert.alert('Stool photos', `${ok} saved${bad ? `, ${bad} need attention` : ''}.`);};
  const discard = async (c: IntakeCard) => {try {await api(`/care/stool-intake/${c.id}`, 'DELETE'); onChange({...intake, items: intake.items.map(x => x.id === c.id ? {...x, status: 'discarded'} : x)});} catch (e: any) {Alert.alert('Not discarded', e?.message || String(e));}};
  const visible = intake.items.filter(c => c.status !== 'discarded');
  const allHandled = visible.length > 0 && visible.every(c => c.status === 'saved' || c.status === 'duplicate');

  return <View style={{gap: 12}}>
    <Card>
      <H3 sub={`${pending.length} to confirm${intake.items.filter(c => c.status === 'duplicate').length ? ` · ${intake.items.filter(c => c.status === 'duplicate').length} already recorded` : ''}. Each photo becomes its own entry at its own time; nothing is saved until you confirm.`}>{visible.length} stool photo{visible.length === 1 ? '' : 's'} detected</H3>
      {Object.keys(byDate).length ? <View style={{backgroundColor: C.paper2, borderRadius: 12, padding: 10, marginTop: 6}}><Text style={{fontWeight: '700', color: C.ink, fontSize: 13}}>These entries will be added to:</Text>{Object.entries(byDate).sort().map(([d, n]) => <Text key={d} style={{color: C.ink, fontSize: 13}}>• {d === 'unknown' ? 'Date to be entered' : fmtDay(d)} — {n}</Text>)}</View> : null}
      <Row style={{marginTop: 10}}>{pending.length > 1 ? <Btn title={busy === 'all' ? 'Saving…' : `Confirm all ${pending.length}`} small onPress={confirmAll}/> : null}<Btn title="Add potty photo(s)" kind="alt" small onPress={onAddMore}/><Btn title={allHandled ? 'Done' : 'Cancel'} kind="ghost" small onPress={allHandled ? onDone : () => {if (pending.length) Alert.alert('Close review?', `${pending.length} photo${pending.length === 1 ? '' : 's'} not confirmed yet. They stay available for 48 hours.`, [{text: 'Keep reviewing'}, {text: 'Close', style: 'destructive', onPress: onCancel}]); else onCancel();}}/></Row>
    </Card>
    {visible.map(c => {
      const d = draft(c); const sug = c.suggestions && !c.suggestions.error ? c.suggestions : null; const weak = c.detected.needs_confirmation;
      const thumb = <Image source={{uri: server + c.image_url, headers: {Authorization: 'Bearer ' + token}}} style={{width: 96, height: 96, borderRadius: 12, backgroundColor: '#eee'}} accessibilityLabel={`Stool photograph ${c.id}`}/>;
      if (c.status === 'saved') return <Card key={c.id} style={{borderColor: C.ok}}><Row>{thumb}<View style={{flex: 1}}><Text style={{fontWeight: '700', color: C.ink}}>✓ Saved · {c.saved_label}</Text><Text style={s.subSmall}>Added to Daily Health, the Care Calendar and stool analytics.</Text></View></Row></Card>;
      if (c.status === 'duplicate') return <Card key={c.id} style={{borderStyle: 'dashed'}}><Row>{thumb}<View style={{flex: 1}}><Text style={{fontWeight: '700', color: C.ink}}>Already recorded</Text><Text style={s.subSmall}>This exact photo is already attached to the stool entry of {c.duplicate_of?.label || 'an existing entry'}. It will not be added twice.</Text><Row style={{marginTop: 8}}>{c.duplicate_of && onOpenEntry ? <Btn title="Open that entry" kind="alt" small onPress={() => onOpenEntry(c.duplicate_of!.entry_id, c.duplicate_of!.at.slice(0, 10))}/> : null}<Btn title="Discard" kind="ghost" small onPress={() => discard(c)}/></Row></View></Row></Card>;
      return <Card key={c.id} style={weak ? {borderColor: C.warn} : undefined}>
        <Row>{thumb}<View style={{flex: 1}}>
          <Text style={{color: C.ink}}><Text style={{fontWeight: '700'}}>Detected: </Text>{c.detected.label}{c.detected.time_unknown ? ' (date only)' : ''}</Text>
          <Text style={s.subSmall}>Source: {c.detected.source_label}{c.detected.confidence && c.detected.confidence !== 'none' ? ` · ${c.detected.confidence} confidence` : ''}{c.detected.camera ? ` · ${c.detected.camera}` : ''}</Text>
          {c.original_filename ? <Text style={[s.subSmall, {fontFamily: 'monospace', fontSize: 11}]}>{c.original_filename}</Text> : null}
          {!weak && !editTime[c.id] ? <TouchableOpacity onPress={() => setEditTime(e => ({...e, [c.id]: true}))}><Text style={{color: C.s1, fontWeight: '700', marginTop: 4}}>Edit time</Text></TouchableOpacity> : null}
        </View></Row>
        {weak ? <Note tone="caution">{c.detected.source === 'none' ? 'No capture time could be read from this photo: enter when it was taken.' : 'This time is a weak guess. Confirm or correct it before saving.'}</Note> : null}
        {(weak || editTime[c.id]) ? <View><Text style={s.label}>When it was taken (YYYY-MM-DDTHH:MM)</Text><TextInput style={s.input} value={d.at} onChangeText={v => set(c.id, {at: v})} placeholder="2026-10-06T08:35" autoCapitalize="none"/></View> : null}
        <Note>{sug ? `${sug.disclaimer || 'Image-based observation only — not a diagnosis.'}${sug.review_message ? ` · ${sug.review_message}` : ''}` : 'Photo saved, but automatic observations could not be generated. Enter what you observed.'}</Note>
        <Row style={{alignItems: 'center'}}><Text style={s.label}>Colour</Text><Ai sug={sug} k="colour"/></Row><Segments items={COLOURS.map(([v, l]) => [v, sug?.colour?.value === v && v ? `${l} ✨` : l] as [string, string])} value={d.colour} onChange={v => set(c.id, {colour: v})}/>
        <Row style={{alignItems: 'center'}}><Text style={s.label}>Consistency</Text><Ai sug={sug} k="consistency"/></Row><Segments items={CONSISTENCY.map(([v, l]) => [v, sug?.consistency?.value === v && v ? `${l} ✨` : l] as [string, string])} value={d.consistency} onChange={v => set(c.id, {consistency: v})}/>
        <Row style={{alignItems: 'center'}}><Text style={s.label}>Amount</Text><Ai sug={sug} k="amount"/></Row><Segments items={AMOUNT.map(([v, l]) => [v, sug?.amount?.value === v && v ? `${l} ✨` : l] as [string, string])} value={d.amount} onChange={v => set(c.id, {amount: v})}/>
        <Row style={{alignItems: 'center'}}><Text style={s.label}>Mucus-like material</Text>{sug?.mucus_like_material ? <Chip label={`AI: ${sug.mucus_like_material.value}`}/> : null}</Row><Segments items={TRI} value={d.mucus} onChange={v => set(c.id, {mucus: v})}/>
        <Row style={{alignItems: 'center'}}><Text style={s.label}>Blood-like colour</Text>{sug?.blood_like_colour ? <Chip label={`AI: ${sug.blood_like_colour.value}`} tone={sug.blood_like_colour.value === 'uncertain' ? 'warn' : 'neutral'}/> : null}</Row><Segments items={TRI} value={d.blood} onChange={v => set(c.id, {blood: v})}/>
        <Check label="Watery appearance" sub={sug?.watery_appearance ? `AI: ${sug.watery_appearance.value}` : undefined} value={d.watery} onChange={() => set(c.id, {watery: !d.watery})}/>
        <Check label="Also log as a stool diaper" value={d.diaper} onChange={() => set(c.id, {diaper: !d.diaper})}/>
        <Text style={s.label}>Your concern</Text><Segments items={CONCERN} value={d.concern} onChange={v => set(c.id, {concern: v})}/>
        <Text style={s.label}>Associated symptoms</Text><TextInput style={s.input} value={d.symptoms} onChangeText={v => set(c.id, {symptoms: v})} placeholder="e.g. fever, vomiting"/>
        <Text style={s.label}>Note</Text><TextInput style={s.input} value={d.note} onChangeText={v => set(c.id, {note: v})} placeholder="Optional"/>
        <Row style={{marginTop: 10}}><Btn title={busy === c.id ? 'Saving…' : 'Confirm & save'} onPress={() => confirmOne(c)}/><Btn title="Discard photo" kind="ghost" small onPress={() => discard(c)}/></Row>
      </Card>;
    })}
    <Text style={s.subSmall}>{intake.disclaimer}</Text>
  </View>;
}
