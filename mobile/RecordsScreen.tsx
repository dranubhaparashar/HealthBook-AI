import React, {useState} from 'react';
import {View, Text, Image, Alert, TextInput} from 'react-native';
import * as ImagePicker from 'expo-image-picker';
import * as DocumentPicker from 'expo-document-picker';
import * as ImageManipulator from 'expo-image-manipulator';
import {Screen, Title, Card, H3, Chip, Chips, Btn, Row, Note, Empty, Segments, Loading, s, C, fmtDate, msg} from './src/ui';

type API = (path: string, method?: string, body?: any, upload?: boolean) => Promise<any>;
const CATEGORIES: [string, string][] = [['prescription', 'Prescription'], ['lab', 'Laboratory report'], ['culture', 'Culture report'], ['vaccination_card', 'Vaccination card'], ['discharge', 'Discharge summary'], ['hospital', 'Hospital record'], ['consultation', 'Consultation'], ['imaging', 'Imaging'], ['other', 'Other']];
const FILTERS: [string, string][] = [['all', 'All'], ['prescription', 'Prescriptions'], ['lab', 'Lab reports'], ['hospital', 'Hospital'], ['vaccination_card', 'Vaccination card'], ['search', 'Search']];
const GROUP: Record<string, string[]> = {prescription: ['prescription', 'consultation'], lab: ['lab', 'culture', 'imaging'], hospital: ['hospital', 'discharge', 'admission', 'birth'], vaccination_card: ['vaccination_card']};

export function RecordsScreen({pid, api, docs, onRefresh, onReview, onOpenDoc, startCamera, onCameraHandled}: {pid: number; api: API; docs: any[]; onRefresh: () => Promise<void>; onReview: (id: number) => void; onOpenDoc: (id: number) => void; startCamera?: 'camera' | 'file' | null; onCameraHandled: () => void}) {
  const [filter, setFilter] = useState('all');
  const [stage, setStage] = useState<'idle' | 'preview' | 'uploading'>('idle');
  const [asset, setAsset] = useState<{uri: string; name: string; type: string; width?: number; height?: number} | null>(null);
  const [category, setCategory] = useState('prescription'); const [title, setTitle] = useState(''); const [recordDate, setRecordDate] = useState('');
  const [query, setQuery] = useState(''); const [found, setFound] = useState<any[] | null>(null); const [busy, setBusy] = useState(false);
  React.useEffect(() => {if (startCamera) {onCameraHandled(); if (startCamera === 'camera') capture(); else pickFile();}}, [startCamera]);
  async function capture() {
    const permission = await ImagePicker.requestCameraPermissionsAsync(); if (permission.status !== 'granted') return Alert.alert('Permission needed', 'Camera permission was not granted.');
    const result = await ImagePicker.launchCameraAsync({mediaTypes: ['images'], quality: .92, allowsEditing: true}); if (result.canceled) return;
    const a = result.assets[0]; setAsset({uri: a.uri, name: 'scan.jpg', type: 'image/jpeg', width: a.width, height: a.height}); setStage('preview');
  }
  async function pickFile() {
    const result = await DocumentPicker.getDocumentAsync({type: ['application/pdf', 'image/jpeg', 'image/png', 'image/heic', 'image/heif'], copyToCacheDirectory: true}); if (result.canceled) return;
    const a = result.assets[0]; setAsset({uri: a.uri, name: a.name || 'document', type: a.mimeType || 'application/octet-stream'}); setTitle(a.name || ''); setStage('preview');
  }
  async function rotate() {if (!asset || asset.type === 'application/pdf') return; const out = await ImageManipulator.manipulateAsync(asset.uri, [{rotate: 90}], {compress: .92, format: ImageManipulator.SaveFormat.JPEG}); setAsset({...asset, uri: out.uri, width: out.width, height: out.height});}
  async function upload() {
    if (!asset) return; setStage('uploading'); setBusy(true);
    try {
      const multi = new FormData(); multi.append('file', {uri: asset.uri, name: asset.name, type: asset.type} as any); multi.append('category', category); multi.append('title', title.trim() || `${CATEGORIES.find(c => c[0] === category)?.[1]} · ${new Date().toISOString().slice(0, 10)}`); if (/^\d{4}-\d{2}-\d{2}$/.test(recordDate)) multi.append('record_date', recordDate);
      const d = await api(`/patients/${pid}/documents`, 'POST', multi, true);
      try {await api(`/documents/${d.id}/extract`, 'POST');} catch (e) {Alert.alert('Saved; OCR unavailable', msg(e) + ' You can transcribe the page manually in the review screen.');}
      await onRefresh(); setStage('idle'); setAsset(null); setTitle(''); setRecordDate('');
      Alert.alert('Saved securely', 'The original is encrypted on the server. Nothing from OCR is verified until you review it.', [{text: 'Review now', onPress: () => onReview(d.id)}, {text: 'Later'}]);
    } catch (e) {setStage('preview'); Alert.alert('Upload not completed', msg(e));} finally {setBusy(false);}
  }
  async function search() {if (!query.trim()) return; setBusy(true); try {const r = await api(`/patients/${pid}/ask`, 'POST', {question: query}); setFound(r.evidence || []); if (!r.evidence?.length) Alert.alert('No matching evidence', r.answer);} catch (e) {Alert.alert('Search failed', msg(e));} finally {setBusy(false);}}
  const rows = filter === 'all' || filter === 'search' ? docs : docs.filter(d => (GROUP[filter] || [filter]).includes(d.category));
  const counts: Record<string, number> = {}; for (const d of docs) counts[d.category] = (counts[d.category] || 0) + 1;
  if (stage !== 'idle' && asset) return <Screen>
    <Title kicker="Scan a report" title="Check the scan" sub="Crop or rotate so the whole page is readable, choose the document type, then upload."/>
    <Card>{asset.type === 'application/pdf' ? <View style={{alignItems: 'center', padding: 20}}><Text style={{fontSize: 40}}>📄</Text><Text style={{fontWeight: '600', color: C.ink}}>{asset.name}</Text></View> : <Image source={{uri: asset.uri}} style={{width: '100%', aspectRatio: asset.width && asset.height ? asset.width / asset.height : 3 / 4, borderRadius: 12, backgroundColor: '#eee'}} resizeMode="contain"/>}
      <Row style={{marginTop: 10}}>{asset.type !== 'application/pdf' ? <Btn title="↻ Rotate" kind="alt" small onPress={rotate}/> : null}<Btn title="Retake" kind="alt" small onPress={capture}/><Btn title="Choose file" kind="ghost" small onPress={pickFile}/></Row></Card>
    <Card><H3>Document type</H3><Segments items={CATEGORIES} value={category} onChange={setCategory}/>
      <Text style={[s.label, {marginTop: 12}]}>Title (optional)</Text><TextInput value={title} onChangeText={setTitle} placeholder="e.g. OPD sheet, Dr Mehta" placeholderTextColor="#9aa7a3" style={s.input}/>
      <Text style={[s.label, {marginTop: 10}]}>Date on the document (YYYY-MM-DD, optional)</Text><TextInput value={recordDate} onChangeText={setRecordDate} placeholder="2026-10-05" placeholderTextColor="#9aa7a3" style={s.input} keyboardType="numbers-and-punctuation"/></Card>
    <Note tone="caution">After upload the page goes through local OCR and opens a review screen. Nothing read from the scan becomes a verified medicine, vaccine or result until you confirm it against the original.</Note>
    <Row><Btn title={stage === 'uploading' ? 'Uploading…' : 'Upload securely'} onPress={upload} disabled={busy}/><Btn title="Cancel" kind="alt" onPress={() => {setStage('idle'); setAsset(null);}}/></Row>
  </Screen>;
  return <Screen>
    <Title kicker="Records" title="Documents" sub={`${docs.length} original pages, encrypted on the server. OCR requires your review.`}/>
    <Row><Btn title="📷 Camera scan" onPress={capture}/><Btn title="Pick PDF / photo" kind="alt" onPress={pickFile}/></Row>
    <Segments items={FILTERS.map(([k, l]) => [k, k === 'all' ? `All (${docs.length})` : l])} value={filter} onChange={setFilter}/>
    {filter === 'search' ? <Card><H3 sub="Evidence retrieval with source pages; never a diagnosis.">Search your records</H3><TextInput value={query} onChangeText={setQuery} placeholder="E. coli, vitamin D, meropenem…" placeholderTextColor="#9aa7a3" style={s.input} returnKeyType="search" onSubmitEditing={search}/><Btn title={busy ? 'Searching…' : 'Search'} onPress={search} disabled={busy}/>
      {found === null ? null : found.length ? found.map((f, i) => <View key={i} style={{paddingVertical: 10, borderTopWidth: 1, borderTopColor: C.line2}}><Chips><Chip label={f.verified ? 'Caregiver verified' : 'Unverified transcription'} tone={f.verified ? 'ok' : 'warn'}/>{f.date ? <Chip label={fmtDate(f.date)}/> : null}</Chips><Text style={{color: C.ink, marginTop: 6}}>{f.statement}</Text>{f.source_document_id ? <Btn title="Open cited page" kind="ghost" small onPress={() => onOpenDoc(f.source_document_id)}/> : null}</View>) : <Empty title="No matching evidence"/>}</Card> : null}
    {rows.length ? rows.map(d => <Card key={d.id}><Row style={{justifyContent: 'space-between'}}><View style={{flex: 1}}><Text style={{fontWeight: '700', color: C.ink, fontSize: 14}} numberOfLines={2}>{d.title}</Text><Text style={s.subSmall}>{d.record_date ? fmtDate(d.record_date) : 'Date not identified'}{d.source_ref ? ` · ${d.source_ref}` : ''} · {d.page_count || 1} page{(d.page_count || 1) > 1 ? 's' : ''}</Text></View><Chip label={d.category.replace('_', ' ')} tone="brand"/></Row>
      <Row style={{marginTop: 10}}><Chip label={d.extraction_status === 'reviewed' ? 'Reviewed' : d.extraction_status === 'needs_review' ? 'OCR · needs review' : 'Not extracted'} tone={d.extraction_status === 'reviewed' ? 'ok' : d.extraction_status === 'needs_review' ? 'warn' : 'neutral'}/><Btn title={d.extraction_status === 'reviewed' ? 'View transcription' : 'Review extraction'} kind="alt" small onPress={() => onReview(d.id)}/><Btn title="Open original" kind="ghost" small onPress={() => onOpenDoc(d.id)}/></Row></Card>) : filter !== 'search' ? <Empty title="No documents in this group" text="Scan a report with the camera or pick a PDF." action={capture} actionLabel="Camera scan"/> : null}
  </Screen>;
}
