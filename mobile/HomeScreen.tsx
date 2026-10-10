import React, {useEffect, useState} from 'react';
import {View, Text} from 'react-native';
import {Screen, Title, Card, H3, Chip, Chips, Btn, Row, Metric, MetricGrid, Note, Empty, Loading, Source, s, C, fmtDate, ageAt, msg} from './src/ui';
import {TouchableOpacity} from 'react-native';
import {dcPreset} from './DailyCareScreen';

type API = (path: string, method?: string, body?: any, upload?: boolean) => Promise<any>;
type Props = {person: {id: number; name: string; dob: string; tz: string}; pid: number; api: API; docs: any[]; meds: any[]; visits: any[]; vaccines: any[]; reminders: any[];
  onUpload: (camera: boolean) => void; onTab: (tab: string) => void; onReview: (id: number) => void; onOpenDoc: (id: number) => void; onDailyCare: () => void; onHealth: (sub: string) => void; onEnablePush: () => void};

export function HomeScreen({person, pid, api, docs, meds, visits, vaccines, reminders, onUpload, onTab, onReview, onOpenDoc, onDailyCare, onHealth, onEnablePush}: Props) {
  const [dash, setDash] = useState<any>(null); const [growth, setGrowth] = useState<any[]>([]); const [err, setErr] = useState('');
  useEffect(() => {let live = true; (async () => {try {const [d, g] = await Promise.all([api(`/patients/${pid}/dashboard`), api(`/patients/${pid}/growth`)]); if (live) {setDash(d); setGrowth(g);}} catch (e) {if (live) setErr(msg(e));}})(); return () => {live = false;};}, [pid, api]);
  const today = dash?.date || new Date().toISOString().slice(0, 10);
  const dueToday = reminders.filter(r => r.at.slice(0, 10) === today);
  const current = meds.filter(m => m.status === 'verified' && (!m.end_date || m.end_date >= today) && !m.actual_stop_date);
  const nextVisit = visits.filter(v => v.confirmed && v.status === 'planned' && v.planned_at.slice(0, 10) >= today).sort((a, b) => a.planned_at.localeCompare(b.planned_at))[0];
  const nv = dash?.vaccination?.next_confirmed; const pend = dash?.vaccination?.pending_due_dates || []; const nextPending = pend.find((x: any) => x.due_date >= today) || pend[0];
  const lastW = [...growth].reverse().find(g => g.weight_kg);
  const recentLab = dash?.labs?.recent?.[0];
  return <Screen>
    <Title kicker="A little care, every day" title={`${person.name}’s care`} sub={`${ageAt(person.dob, today) || '—'} old · born ${fmtDate(person.dob)}`}/>
    <Row><Btn title="📷 Scan a report" onPress={() => onUpload(true)}/><Btn title="Upload file" kind="alt" onPress={() => onUpload(false)}/></Row>
    {err ? <Note tone="caution">{err}</Note> : null}
    <MetricGrid>
      <Metric label="Current age" value={ageAt(person.dob, today) || '—'} sub={`Born ${fmtDate(person.dob)}`} accent/>
      <Metric label="Today’s care" value={dueToday.length} sub={dueToday.length ? 'confirmed reminders' : 'No confirmed reminders today'} onPress={() => onHealth('medicines')}/>
      <Metric label="Next vaccination" value={nv ? fmtDate(nv.due_date).slice(0, 6) : nextPending ? fmtDate(nextPending.due_date).slice(0, 6) : '—'} sub={nv ? `${nv.name} ${nv.dose_label} · confirmed` : nextPending ? `${nextPending.vaccines.join(', ')} · card date, verify` : 'No upcoming card date'} onPress={() => onHealth('vaccines')}/>
      <Metric label="Verified medicines" value={current.length} sub={`${meds.filter(m => m.status === 'draft').length} drafts awaiting your check`} onPress={() => onHealth('medicines')}/>
      <Metric label="Latest weight" value={lastW ? Number(lastW.weight_kg).toFixed(2).replace(/\.?0+$/, '') : '—'} unit={lastW ? 'kg' : ''} sub={lastW ? `${fmtDate(lastW.record_date)} · age ${ageAt(person.dob, lastW.record_date)}` : 'No measurement yet'} onPress={() => onHealth('growth')}/>
      <Metric label="Pending reviews" value={dash?.reviews_pending ?? docs.filter(d => d.extraction_status !== 'reviewed').length} sub="Unverified rows never create reminders" onPress={() => onTab('RECORDS')}/>
    </MetricGrid>
    <Card><H3 sub={`${fmtDate(today)} · confirmed items only`}>Today’s care schedule</H3>{dueToday.length ? dueToday.map(r => <View key={r.key} style={{paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.line2}}><Text style={{fontWeight: '600', color: C.ink}}>{r.title}</Text><Text style={s.subSmall}>{r.at.slice(11, 16)} · {r.kind}</Text></View>) : <Empty title="No confirmed care reminders today" text="Verified prescriptions, confirmed appointments and confirmed vaccination dates appear here."/>}</Card>
    <Card><H3 sub="Appointments are not administration records.">Upcoming hospital visit</H3>{nextVisit ? <View><Text style={{fontWeight: '600', color: C.ink, fontSize: 15}}>{nextVisit.facility || 'Hospital'}</Text><Text style={s.subSmall}>{nextVisit.planned_at.replace('T', ' · ').slice(0, 18)} · {nextVisit.purpose}</Text><Chips><Chip label="Confirmed appointment" tone="info"/></Chips></View> : <Empty title="Nothing scheduled" text="Add the appointment confirmed by your hospital." action={() => onHealth('hospital')} actionLabel="Hospital history"/>}</Card>
    <Card><H3>Current verified medicines</H3>{current.length ? current.map(m => <View key={m.id} style={{paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.line2}}><Text style={{fontWeight: '600', color: C.ink}}>{m.name}</Text><Text style={s.subSmall}>{fmtDate(m.start_date)} – {fmtDate(m.end_date)} · {m.reported_given_count || 0} caregiver-reported doses</Text></View>) : <Empty title="No verified current courses" text="Drafts stay silent until you verify them against the prescription."/>}</Card>
    <Card><H3>Recent laboratory result</H3>{dash === null ? <Loading/> : recentLab ? <View><Text style={{fontWeight: '600', color: C.ink, fontSize: 15}}>{recentLab.test_name} · {recentLab.value} {recentLab.unit}</Text><Text style={s.subSmall}>{fmtDate(recentLab.record_date)}{recentLab.reference_range ? ` · printed ref ${recentLab.reference_range}` : ''}</Text><Row style={{marginTop: 8}}><Chip label={recentLab.verified ? 'Verified' : 'Unverified'} tone={recentLab.verified ? 'ok' : 'warn'}/><Source id={recentLab.source_document_id} page={recentLab.source_page} onOpen={onOpenDoc}/><Btn title="All labs" kind="ghost" small onPress={() => onHealth('labs')}/></Row></View> : <Empty title="No laboratory values yet"/>}</Card>
    <Card><H3 sub="One tap opens the entry sheet. Scheduled medicines are never marked given automatically.">Today’s care · quick add</H3><View style={{flexDirection: 'row', flexWrap: 'wrap', gap: 8}}>{([['fluid', '+ Water', '💧', '#dfeef6'], ['diaper', '+ Diaper', '🧷', '#f7ecd2'], ['stool', '+ Potty photo', '📷', '#efe3da'], ['medicine', '+ Medicine', '💊', '#e3efeb'], ['symptom', '+ Fever', '🌡', '#f6e3dc'], ['activity', '+ Activity', '🧸', '#e6efdc']] as const).map(([k, l, ic, bg]) => <TouchableOpacity key={k} accessibilityRole="button" accessibilityLabel={l} onPress={() => {dcPreset.sheet = k; onDailyCare();}} style={{width: '31%', minHeight: 78, borderRadius: 16, alignItems: 'center', justifyContent: 'center', gap: 4, borderWidth: 1, borderColor: C.line, backgroundColor: bg}}><Text style={{fontSize: 22}}>{ic}</Text><Text style={{fontWeight: '800', fontSize: 12, color: C.ink}}>{l}</Text></TouchableOpacity>)}</View><Btn title="Open the daily care journal →" kind="alt" onPress={onDailyCare}/></Card>
    <Card><H3>Recent reports</H3>{docs.slice(0, 4).map(d => <Row key={d.id} style={{paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.line2, justifyContent: 'space-between'}}><View style={{flex: 1}}><Text style={{fontWeight: '600', color: C.ink}} numberOfLines={1}>{d.title}</Text><Text style={s.subSmall}>{fmtDate(d.record_date)} · {d.category.replace('_', ' ')}</Text></View><Btn title={d.extraction_status === 'reviewed' ? 'View' : 'Review'} kind="alt" small onPress={() => onReview(d.id)}/></Row>)}{!docs.length ? <Empty title="No reports yet" text="Scan the first prescription or vaccination card." action={() => onUpload(true)} actionLabel="Scan a report"/> : null}</Card>
    <Btn title="Enable reminder alerts on this phone" kind="alt" onPress={onEnablePush}/>
    <Note>{dash?.safety || 'Counts come from stored records. Pending items are unverified transcriptions and never generate reminders.'}</Note>
  </Screen>;
}
