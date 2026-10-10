import React from 'react';
import {View, Text, TouchableOpacity, StyleSheet, TextInput, ActivityIndicator, ViewStyle, StyleProp} from 'react-native';

export const C = {bg: '#f7f5f0', paper: '#fffefb', paper2: '#f3f1ea', ink: '#223139', ink2: '#4d5c63', muted: '#75848a', line: '#e3e5dd', line2: '#eceee7',
  s1: '#0a8c7f', s1Soft: '#e2f2ef', s2: '#b06d0c', s2Soft: '#f6ead6', s3: '#3b6fc6', s3Soft: '#e4ecf8', s4: '#b34d86', s4Soft: '#f4e3ec',
  ok: '#2d7a57', okSoft: '#dff0e6', warn: '#9a631b', warnSoft: '#fbefd9', bad: '#a4524b', badSoft: '#f8e4e1', neutral: '#66757b', neutralSoft: '#eef0ec'};
export type Tone = 'ok' | 'warn' | 'bad' | 'info' | 'plum' | 'neutral' | 'brand';
const toneBg: Record<Tone, [string, string]> = {ok: [C.okSoft, C.ok], warn: [C.warnSoft, C.warn], bad: [C.badSoft, C.bad], info: [C.s3Soft, C.s3], plum: [C.s4Soft, C.s4], neutral: [C.neutralSoft, C.neutral], brand: [C.s1Soft, C.s1]};
export const fmtDate = (d?: string | null) => d ? new Date(String(d).slice(0, 10) + 'T12:00:00').toLocaleDateString('en-IN', {day: '2-digit', month: 'short', year: 'numeric'}) : 'Not recorded';
export const fmtShort = (d?: string | null) => d ? new Date(String(d).slice(0, 10) + 'T12:00:00').toLocaleDateString('en-IN', {day: '2-digit', month: 'short'}) : '—';
export const ageAt = (dob?: string, on?: string | null) => {
  if (!dob || !on) return '';
  const a = new Date(dob + 'T12:00:00'), b = new Date(String(on).slice(0, 10) + 'T12:00:00');
  const days = Math.round((b.getTime() - a.getTime()) / 864e5);
  if (days < 0) return ''; if (days < 56) return days === 0 ? 'birth' : `${days} d`; if (days < 120) return `${Math.floor(days / 7)} wk`;
  let m = (b.getFullYear() - a.getFullYear()) * 12 + b.getMonth() - a.getMonth() - (b.getDate() < a.getDate() ? 1 : 0);
  return m < 24 ? `${m} mo` : `${Math.floor(m / 12)} y ${m % 12} mo`;
};
export const msg = (e: unknown) => e instanceof Error ? e.message : String(e);

export const Screen = ({children}: {children: React.ReactNode}) => <View style={{gap: 14, paddingBottom: 20}}>{children}</View>;
export const Title = ({kicker, title, sub}: {kicker?: string; title: string; sub?: string}) => (
  <View style={{marginBottom: 4}}>{kicker ? <Text style={s.kicker}>{kicker.toUpperCase()}</Text> : null}<Text style={s.h1}>{title}</Text>{sub ? <Text style={s.sub}>{sub}</Text> : null}</View>);
export const Card = ({children, style, onPress}: {children: React.ReactNode; style?: StyleProp<ViewStyle>; onPress?: () => void}) => onPress
  ? <TouchableOpacity accessibilityRole="button" onPress={onPress} style={[s.card, style]}>{children}</TouchableOpacity>
  : <View style={[s.card, style]}>{children}</View>;
export const H3 = ({children, sub}: {children: React.ReactNode; sub?: string}) => <View style={{marginBottom: 8}}><Text style={s.h3}>{children}</Text>{sub ? <Text style={s.subSmall}>{sub}</Text> : null}</View>;
export const Chip = ({label, tone = 'neutral'}: {label: string; tone?: Tone}) => <View style={[s.chip, {backgroundColor: toneBg[tone][0]}]}><View style={[s.dot, {backgroundColor: toneBg[tone][1]}]}/><Text style={[s.chipText, {color: toneBg[tone][1]}]}>{label}</Text></View>;
export const Chips = ({children}: {children: React.ReactNode}) => <View style={s.chips}>{children}</View>;
export const Btn = ({title, onPress, kind = 'primary', small = false, disabled = false}: {title: string; onPress: () => void; kind?: 'primary' | 'alt' | 'ghost' | 'danger'; small?: boolean; disabled?: boolean}) => (
  <TouchableOpacity accessibilityRole="button" disabled={disabled} onPress={onPress} style={[s.btn, kind === 'alt' && s.btnAlt, kind === 'ghost' && s.btnGhost, kind === 'danger' && s.btnDanger, small && s.btnSmall, disabled && {opacity: .5}]}>
    <Text style={[s.btnText, (kind === 'alt' || kind === 'ghost') && {color: C.s1}, kind === 'danger' && {color: '#fff'}, small && {fontSize: 12}]}>{title}</Text></TouchableOpacity>);
export const Row = ({children, style}: {children: React.ReactNode; style?: StyleProp<ViewStyle>}) => <View style={[{flexDirection: 'row', gap: 8, flexWrap: 'wrap', alignItems: 'center'}, style]}>{children}</View>;
export const Metric = ({label, value, unit, sub, onPress, accent}: {label: string; value: string | number; unit?: string; sub?: string; onPress?: () => void; accent?: boolean}) => (
  <Card onPress={onPress} style={[s.metric, accent && {backgroundColor: C.s1Soft}]}><Text style={s.kicker}>{label.toUpperCase()}</Text><View style={{flexDirection: 'row', alignItems: 'baseline', gap: 4}}><Text style={s.metricValue}>{String(value)}</Text>{unit ? <Text style={s.metricUnit}>{unit}</Text> : null}</View>{sub ? <Text style={s.subSmall}>{sub}</Text> : null}</Card>);
export const MetricGrid = ({children}: {children: React.ReactNode}) => <View style={s.metricGrid}>{children}</View>;
export const Note = ({children, tone = 'neutral'}: {children: React.ReactNode; tone?: 'neutral' | 'caution'}) => <View style={[s.note, tone === 'caution' && {backgroundColor: C.warnSoft}]}><Text style={s.noteText}>{children}</Text></View>;
export const Empty = ({title, text, action, actionLabel}: {title: string; text?: string; action?: () => void; actionLabel?: string}) => (
  <View style={s.empty}><Text style={{fontWeight: '700', color: C.ink2}}>{title}</Text>{text ? <Text style={[s.subSmall, {textAlign: 'center'}]}>{text}</Text> : null}{action ? <Btn title={actionLabel || 'Add'} onPress={action} kind="alt" small/> : null}</View>);
export const Input = ({label, value, onChange, placeholder = '', multiline = false, keyboard}: {label: string; value: string; onChange: (v: string) => void; placeholder?: string; multiline?: boolean; keyboard?: 'default' | 'numeric' | 'email-address'}) => (
  <View style={{marginBottom: 10}}><Text style={s.label}>{label}</Text><TextInput value={value} onChangeText={onChange} placeholder={placeholder} placeholderTextColor="#9aa7a3" multiline={multiline} keyboardType={keyboard || 'default'} autoCapitalize="none" style={[s.input, multiline && {minHeight: 76, textAlignVertical: 'top'}]}/></View>);
export const Check = ({label, sub, value, onChange}: {label: string; sub?: string; value: boolean; onChange: () => void}) => (
  <TouchableOpacity accessibilityRole="checkbox" accessibilityState={{checked: value}} onPress={onChange} style={[s.check, !value && {opacity: .6}]}><View style={[s.box, value && {backgroundColor: C.s1, borderColor: C.s1}]}>{value ? <Text style={{color: '#fff', fontWeight: '800', fontSize: 13}}>✓</Text> : null}</View><View style={{flex: 1}}><Text style={{fontWeight: '600', color: C.ink, fontSize: 14}}>{label}</Text>{sub ? <Text style={s.subSmall}>{sub}</Text> : null}</View></TouchableOpacity>);
export const Fact = ({k, v}: {k: string; v: React.ReactNode}) => <View style={s.fact}><Text style={s.kicker}>{k.toUpperCase()}</Text>{typeof v === 'string' ? <Text style={{color: C.ink, fontSize: 13, lineHeight: 19}}>{v}</Text> : v}</View>;
export const Facts = ({children}: {children: React.ReactNode}) => <View style={{gap: 8}}>{children}</View>;
export const Source = ({id, page, onOpen}: {id?: number | null; page?: number; onOpen: (id: number) => void}) => id
  ? <TouchableOpacity accessibilityRole="button" onPress={() => onOpen(id)} style={s.src}><Text style={{color: C.s1, fontSize: 12}}>▣</Text><Text style={{fontSize: 12, fontWeight: '600', color: C.ink2}}>Source{page && page > 1 ? ` p.${page}` : ''}</Text></TouchableOpacity>
  : <Text style={[s.subSmall, {fontStyle: 'italic'}]}>No source attached</Text>;
export const Loading = ({text = 'Loading…'}: {text?: string}) => <View style={{padding: 24, alignItems: 'center', gap: 8}}><ActivityIndicator color={C.s1}/><Text style={s.subSmall}>{text}</Text></View>;
export const Segments = ({items, value, onChange}: {items: [string, string][]; value: string; onChange: (k: string) => void}) => (
  <View style={s.segs}>{items.map(([k, l]) => <TouchableOpacity key={k} accessibilityRole="tab" onPress={() => onChange(k)} style={[s.seg, value === k && s.segOn]}><Text style={[s.segText, value === k && {color: '#fff'}]}>{l}</Text></TouchableOpacity>)}</View>);
export const Divider = () => <View style={{height: 1, backgroundColor: C.line2, marginVertical: 10}}/>;
export const Insight = ({k, v}: {k: string; v: string}) => <View style={s.insight}><Text style={[s.subSmall, {flex: 1}]}>{k}</Text><Text style={{fontWeight: '600', color: C.ink, fontSize: 13, textAlign: 'right', flex: 1}}>{v}</Text></View>;

export const s = StyleSheet.create({
  kicker: {fontSize: 10.5, fontWeight: '800', letterSpacing: 1.3, color: C.muted, marginBottom: 4},
  h1: {fontSize: 26, fontWeight: '700', color: C.ink, letterSpacing: -.6, lineHeight: 31},
  h3: {fontSize: 16, fontWeight: '700', color: C.ink, letterSpacing: -.2},
  sub: {color: C.muted, fontSize: 13.5, lineHeight: 19, marginTop: 4},
  subSmall: {color: C.muted, fontSize: 12.5, lineHeight: 18, marginTop: 2},
  card: {backgroundColor: C.paper, borderWidth: 1, borderColor: C.line, borderRadius: 18, padding: 16, shadowColor: '#223139', shadowOpacity: .06, shadowRadius: 12, shadowOffset: {width: 0, height: 6}, elevation: 2},
  chip: {flexDirection: 'row', alignItems: 'center', gap: 5, paddingVertical: 4, paddingHorizontal: 10, borderRadius: 100},
  chipText: {fontSize: 11.5, fontWeight: '700'},
  dot: {width: 6, height: 6, borderRadius: 3},
  chips: {flexDirection: 'row', flexWrap: 'wrap', gap: 6, alignItems: 'center'},
  btn: {backgroundColor: C.s1, borderRadius: 12, paddingVertical: 12, paddingHorizontal: 16, alignItems: 'center', justifyContent: 'center', minHeight: 46, borderWidth: 1, borderColor: C.s1},
  btnAlt: {backgroundColor: C.paper, borderColor: C.line},
  btnGhost: {backgroundColor: 'transparent', borderColor: 'transparent'},
  btnDanger: {backgroundColor: C.bad, borderColor: C.bad},
  btnSmall: {paddingVertical: 8, paddingHorizontal: 12, minHeight: 38},
  btnText: {color: '#fff', fontWeight: '700', fontSize: 13.5},
  metric: {minWidth: '47%', flex: 1, padding: 14, minHeight: 104},
  metricGrid: {flexDirection: 'row', flexWrap: 'wrap', gap: 10},
  metricValue: {fontSize: 26, fontWeight: '600', color: C.ink, letterSpacing: -.8, lineHeight: 32},
  metricUnit: {fontSize: 12, fontWeight: '600', color: C.muted},
  note: {backgroundColor: C.paper2, borderRadius: 12, padding: 12, borderWidth: 1, borderColor: C.line2},
  noteText: {color: C.ink2, fontSize: 12.5, lineHeight: 18},
  empty: {alignItems: 'center', gap: 8, padding: 22, borderWidth: 1, borderStyle: 'dashed', borderColor: C.line, borderRadius: 14, backgroundColor: C.paper2},
  label: {fontSize: 12, fontWeight: '700', color: C.ink2, marginBottom: 5},
  input: {borderWidth: 1, borderColor: C.line, borderRadius: 12, padding: 12, backgroundColor: C.paper, color: C.ink, fontSize: 14, minHeight: 46},
  check: {flexDirection: 'row', gap: 12, alignItems: 'flex-start', padding: 12, borderWidth: 1, borderColor: C.line2, borderRadius: 12, backgroundColor: C.paper},
  box: {width: 22, height: 22, borderRadius: 6, borderWidth: 1.5, borderColor: C.line, alignItems: 'center', justifyContent: 'center', backgroundColor: C.paper, marginTop: 1},
  fact: {backgroundColor: C.paper2, borderRadius: 12, padding: 11, borderWidth: 1, borderColor: C.line2},
  src: {flexDirection: 'row', gap: 6, alignItems: 'center', alignSelf: 'flex-start', paddingVertical: 5, paddingHorizontal: 9, borderRadius: 8, borderWidth: 1, borderColor: C.line, backgroundColor: C.paper2},
  segs: {flexDirection: 'row', flexWrap: 'wrap', gap: 6},
  seg: {paddingVertical: 8, paddingHorizontal: 13, borderRadius: 100, borderWidth: 1, borderColor: C.line, backgroundColor: C.paper},
  segOn: {backgroundColor: C.s1, borderColor: C.s1},
  segText: {fontSize: 12.5, fontWeight: '700', color: C.ink2},
  insight: {flexDirection: 'row', gap: 12, paddingVertical: 8, borderTopWidth: 1, borderTopColor: C.line2},
});
