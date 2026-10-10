/* Touch-first chart components for the phone (react-native-svg). Same rules as the web chart system:
   one series per chart, thin marks, no inferred values, a single measurement is a value card rather than a stretched line. */
import React, {useMemo, useState} from 'react';
import {View, Text, StyleSheet, LayoutChangeEvent, TouchableOpacity} from 'react-native';
import Svg, {Path, Circle, Line, Rect, Text as SvgText, G} from 'react-native-svg';
import {C, ageAt, fmtDate, fmtShort} from './ui';

export type Point = {date: string; value: number | null; display?: string; unit?: string; verified?: boolean; separate?: boolean; note?: string; sourceId?: number | null; ref?: string};
const DAY = 864e5;
const t = (iso: string) => new Date(iso.slice(0, 10) + 'T12:00:00').getTime();
const fmtNum = (v: number) => {const a = Math.abs(v); const d = a >= 100 ? 0 : a >= 10 ? 1 : a >= 1 ? 2 : 3; return v.toFixed(d).replace(/\.?0+$/, '') || '0';};
function nice(lo: number, hi: number, count = 4) {
  if (!(hi > lo)) {const pad = Math.abs(lo) * .08 || 1; lo -= pad; hi += pad;}
  const raw = (hi - lo) / count, mag = Math.pow(10, Math.floor(Math.log10(raw))), r = raw / mag, step = (r <= 1.5 ? 1 : r <= 3 ? 2 : r <= 7 ? 5 : 10) * mag;
  const nlo = Math.floor(lo / step) * step, nhi = Math.ceil(hi / step) * step; const ticks: number[] = []; for (let v = nlo; v <= nhi + step / 2; v += step) ticks.push(+v.toFixed(10)); return {lo: nlo, hi: nhi, ticks};
}
function dateTicks(t0: number, t1: number, max = 4) {
  const out: {t: number; label: string}[] = []; const days = (t1 - t0) / DAY;
  if (days <= 45) {const step = days <= 10 ? 2 : days <= 21 ? 5 : 10; for (let d = t0; d <= t1; d += step * DAY) out.push({t: d, label: fmtShort(new Date(d).toISOString())}); return out;}
  const a = new Date(t0), b = new Date(t1); const months = (b.getFullYear() - a.getFullYear()) * 12 + b.getMonth() - a.getMonth() + 1; const stepM = months <= max ? 1 : months <= max * 2 ? 2 : months <= max * 3 ? 3 : months <= max * 6 ? 6 : 12;
  for (let m = new Date(a.getFullYear(), a.getMonth(), 1, 12); m <= b; m.setMonth(m.getMonth() + stepM)) if (m.getTime() >= t0) out.push({t: m.getTime(), label: m.toLocaleString('en-IN', {month: 'short', year: '2-digit'})});
  return out;
}

export function SingleValue({point, unit, dob, label}: {point: Point; unit: string; dob?: string; label: string}) {
  return <View style={st.single}><View style={{flexDirection: 'row', alignItems: 'baseline', gap: 4}}><Text style={st.singleValue}>{point.display ?? fmtNum(point.value ?? 0)}</Text><Text style={st.unit}>{unit}</Text></View>
    <Text style={st.singleLabel}>{label}</Text><Text style={st.muted}>{fmtDate(point.date)}{dob ? ` · age ${ageAt(dob, point.date)}` : ''}</Text>
    <View style={st.pill}><Text style={st.pillText}>More measurements are needed before a trend can be shown.</Text></View></View>;
}

export function TrendChart({points, unit, dob, height = 200, label = 'Value', lineVerifiedOnly = false, onPointPress}: {points: Point[]; unit: string; dob?: string; height?: number; label?: string; lineVerifiedOnly?: boolean; onPointPress?: (p: Point) => void}) {
  const [width, setWidth] = useState(0);
  const [sel, setSel] = useState<number | null>(null);
  const pts = useMemo(() => points.filter(p => p.value !== null && Number.isFinite(Number(p.value)) && p.date).map(p => ({...p, v: Number(p.value)})).sort((a, b) => t(a.date) - t(b.date) || a.v - b.v), [points]);
  if (!pts.length) return <View style={st.empty}><Text style={st.muted}>No numeric values to chart.</Text></View>;
  if (pts.length === 1) return <SingleValue point={pts[0]} unit={unit} dob={dob} label={`${label} recorded once`}/>;
  const L = 42, R = 14, T = 18, B = dob ? 40 : 28; const W = Math.max(width, 200), H = height;
  const vals = pts.map(p => p.v); let lo = Math.min(...vals), hi = Math.max(...vals); const padV = Math.max((hi - lo) * .25, Math.abs(hi) * .06, .05); lo -= padV; hi += padV; if (Math.min(...vals) >= 0 && lo < 0) lo = 0;
  const sc = nice(lo, hi, 3); let t0 = t(pts[0].date), t1 = t(pts[pts.length - 1].date); const minSpan = pts.length <= 3 ? DAY * 45 : DAY * 30;
  if (t1 - t0 < minSpan) {const c = (t0 + t1) / 2; t0 = c - minSpan / 2; t1 = c + minSpan / 2;} else {const pad = (t1 - t0) * (pts.length <= 3 ? .14 : .06); t0 -= pad; t1 += pad;}
  const x = (d: string) => L + (t(d) - t0) / (t1 - t0) * (W - L - R), y = (v: number) => T + (sc.hi - v) / (sc.hi - sc.lo || 1) * (H - T - B);
  const linePts = lineVerifiedOnly && pts.filter(p => p.verified !== false).length >= 2 ? pts.filter(p => p.verified !== false) : pts;
  const path = linePts.map((p, i) => `${i ? 'L' : 'M'}${x(p.date).toFixed(1)} ${y(p.v).toFixed(1)}`).join(' ');
  const area = `${path} L${x(linePts[linePts.length - 1].date).toFixed(1)} ${H - B} L${x(linePts[0].date).toFixed(1)} ${H - B} Z`;
  const xt = dateTicks(t0, t1, 4); const last = pts.length - 1; const iMax = vals.indexOf(Math.max(...vals)), iMin = vals.indexOf(Math.min(...vals));
  const labelled = new Set([last, ...(pts.length >= 4 ? [iMax, iMin] : [])]);
  const pick = (lx: number) => {let best = 0, bd = 1e9; pts.forEach((p, i) => {const d = Math.abs(x(p.date) - lx); if (d < bd) {bd = d; best = i;}}); setSel(best);};
  const cur = sel !== null ? pts[sel] : null;
  return <View onLayout={(e: LayoutChangeEvent) => setWidth(e.nativeEvent.layout.width)}>
    {cur ? <View style={st.tip}><Text style={st.tipValue}>{cur.display ?? fmtNum(cur.v)} {cur.unit || unit}</Text><Text style={st.tipMeta}>{fmtDate(cur.date)}{dob ? ` · age ${ageAt(dob, cur.date)}` : ''}{cur.verified === false ? ' · unverified' : ''}{cur.separate ? ' · separate same-day report' : ''}</Text>{cur.ref || cur.note ? <Text style={st.tipMeta}>{[cur.ref, cur.note].filter(Boolean).join(' · ')}</Text> : null}{onPointPress && cur.sourceId ? <TouchableOpacity onPress={() => onPointPress(cur)}><Text style={st.tipLink}>Open source page →</Text></TouchableOpacity> : null}</View> : <Text style={st.hint}>Touch the chart to read a value</Text>}
    {width > 0 && <View onStartShouldSetResponder={() => true} onResponderGrant={e => pick(e.nativeEvent.locationX)} onResponderMove={e => pick(e.nativeEvent.locationX)}>
      <Svg width={W} height={H}>
        {sc.ticks.map(v => <G key={v}><Line x1={L} x2={W - R} y1={y(v)} y2={y(v)} stroke={C.line2} strokeWidth={1}/><SvgText x={L - 6} y={y(v) + 3.5} fontSize={10} fill={C.muted} textAnchor="end">{fmtNum(v)}</SvgText></G>)}
        {xt.map(k => <G key={k.t}><SvgText x={x(new Date(k.t).toISOString())} y={H - B + 14} fontSize={10} fill={C.muted} textAnchor="middle">{k.label}</SvgText>{dob ? <SvgText x={x(new Date(k.t).toISOString())} y={H - B + 26} fontSize={9} fill={C.muted} textAnchor="middle">{ageAt(dob, new Date(k.t).toISOString())}</SvgText> : null}</G>)}
        <SvgText x={L - 6} y={H - B + 14} fontSize={10} fontWeight="700" fill={C.ink2} textAnchor="end">{unit}</SvgText>
        {linePts.length >= 2 && <Path d={area} fill={C.s1} opacity={.08}/>}
        {linePts.length >= 2 && <Path d={path} fill="none" stroke={C.s1} strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"/>}
        {cur && <Line x1={x(cur.date)} x2={x(cur.date)} y1={T} y2={H - B} stroke={C.ink2} strokeWidth={1} opacity={.5}/>}
        {pts.map((p, i) => <G key={i}><Circle cx={x(p.date)} cy={y(p.v)} r={sel === i ? 7 : 4.5} fill={p.verified === false ? C.paper : C.s1} stroke={p.separate ? C.s2 : p.verified === false ? C.s1 : C.paper} strokeWidth={2}/>
          {labelled.has(i) && sel === null ? <SvgText x={x(p.date)} y={y(p.v) - 10} fontSize={10.5} fontWeight="700" fill={C.ink} textAnchor={i === last ? 'end' : i === 0 ? 'start' : 'middle'}>{fmtNum(p.v)}</SvgText> : null}</G>)}
      </Svg></View>}
  </View>;
}

export type SpanRow = {label: string; sub?: string; start: string; end?: string | null; kind: 'fever' | 'parent' | 'abx' | 'iv' | 'pending' | 'stay' | 'episode' | 'given' | 'due' | 'overdue' | 'default'; approx?: boolean; onPress?: () => void};
const kindColor: Record<SpanRow['kind'], string> = {fever: C.s4, parent: C.s2, abx: C.s1, iv: C.s3, pending: C.s2, stay: C.s3, episode: C.s3, given: C.s1, due: C.s3, overdue: C.bad, default: C.s1};
export function SpanTimeline({rows, start, end, today, spans = []}: {rows: SpanRow[]; start?: string; end?: string; today?: string; spans?: {start: string; end: string; kind: 'stay'}[]}) {
  const [width, setWidth] = useState(0);
  const valid = rows.filter(r => r.start);
  if (!valid.length) return <View style={st.empty}><Text style={st.muted}>No dated records yet.</Text></View>;
  const all = valid.flatMap(r => [t(r.start), t(r.end || r.start)]); let t0 = start ? t(start) : Math.min(...all), t1 = end ? t(end) : Math.max(...all); if (t1 - t0 < DAY * 30) {t0 -= DAY * 10; t1 += DAY * 20;}
  const L = 118, R = 10, T = 22, rowH = 30, W = Math.max(width, 240), H = T + valid.length * rowH + 26; const x = (d: string) => L + (t(d) - t0) / (t1 - t0) * (W - L - R);
  const xt = dateTicks(t0, t1, 4);
  return <View onLayout={e => setWidth(e.nativeEvent.layout.width)}>{width > 0 && <Svg width={W} height={H}>
    {spans.map((sp, i) => {const a = Math.max(x(sp.start), L), b = Math.min(x(sp.end), W - R); return b > a ? <Rect key={i} x={a} y={T - 4} width={b - a} height={H - T - 20} fill={C.s3} opacity={.09}/> : null;})}
    {xt.map(k => <G key={k.t}><Line x1={x(new Date(k.t).toISOString())} x2={x(new Date(k.t).toISOString())} y1={T - 4} y2={H - 22} stroke={C.line2}/><SvgText x={x(new Date(k.t).toISOString())} y={H - 8} fontSize={10} fill={C.muted} textAnchor="middle">{k.label}</SvgText></G>)}
    {today && t(today) >= t0 && t(today) <= t1 ? <G><Line x1={x(today)} x2={x(today)} y1={T - 8} y2={H - 22} stroke={C.ink2} opacity={.5}/><SvgText x={x(today)} y={T - 11} fontSize={9} fontWeight="700" fill={C.ink2} textAnchor="middle">Today</SvgText></G> : null}
    {valid.map((r, i) => {const yy = T + i * rowH; const a = Math.max(L, Math.min(x(r.start), W - R)), b = Math.max(a + 8, Math.min(x(r.end || r.start), W - R)); const dot = !r.end || r.end === r.start; const col = kindColor[r.kind];
      return <G key={i} onPress={r.onPress}><Rect x={0} y={yy} width={W} height={rowH} fill="transparent"/><SvgText x={0} y={yy + rowH / 2 + (r.sub ? 1 : 4)} fontSize={11.5} fontWeight="600" fill={C.ink}>{r.label.length > 16 ? r.label.slice(0, 15) + '…' : r.label}</SvgText>{r.sub ? <SvgText x={0} y={yy + rowH / 2 + 12} fontSize={9} fill={C.muted}>{r.sub.length > 22 ? r.sub.slice(0, 21) + '…' : r.sub}</SvgText> : null}
        {dot ? <Circle cx={a} cy={yy + rowH / 2} r={5.5} fill={col} stroke={C.paper} strokeWidth={2}/> : <Rect x={a} y={yy + rowH / 2 - 6} width={b - a} height={12} rx={4} fill={col} opacity={r.kind === 'pending' ? .55 : 1} stroke={r.approx ? col : 'none'} strokeDasharray={r.approx ? '3 2' : undefined}/>}</G>;})}
  </Svg>}</View>;
}

export function Bars({items, unit = ''}: {items: {label: string; value: number; tone?: 'ok' | 'brand'}[]; unit?: string}) {
  const [width, setWidth] = useState(0);
  if (!items.length) return <View style={st.empty}><Text style={st.muted}>Nothing documented.</Text></View>;
  const max = Math.max(...items.map(i => i.value), 1e-9); const L = 120, R = 40, rowH = 28, W = Math.max(width, 240), H = items.length * rowH + 6;
  return <View onLayout={e => setWidth(e.nativeEvent.layout.width)}>{width > 0 && <Svg width={W} height={H}>{items.map((it, i) => {const w = Math.max(3, (W - L - R) * it.value / max); const yy = i * rowH + 3;
    return <G key={i}><SvgText x={0} y={yy + 14} fontSize={11.5} fontWeight="600" fill={C.ink}>{it.label.length > 17 ? it.label.slice(0, 16) + '…' : it.label}</SvgText><Rect x={L} y={yy + 3} width={w} height={14} rx={4} fill={it.tone === 'ok' ? C.ok : C.s1}/><SvgText x={L + w + 6} y={yy + 14} fontSize={11} fontWeight="700" fill={C.ink}>{it.value}{unit}</SvgText></G>;})}</Svg>}</View>;
}

const st = StyleSheet.create({
  single: {backgroundColor: C.paper2, borderWidth: 1, borderColor: C.line2, borderRadius: 14, padding: 14, gap: 4},
  singleValue: {fontSize: 32, fontWeight: '600', color: C.ink, letterSpacing: -1}, unit: {fontSize: 13, color: C.muted, fontWeight: '600'},
  singleLabel: {fontWeight: '700', color: C.ink, fontSize: 13.5}, muted: {color: C.muted, fontSize: 12.5},
  pill: {alignSelf: 'flex-start', backgroundColor: C.paper, borderWidth: 1, borderColor: C.line, borderRadius: 8, paddingVertical: 5, paddingHorizontal: 9, marginTop: 6}, pillText: {fontSize: 12, fontWeight: '600', color: C.ink2},
  empty: {padding: 18, alignItems: 'center', borderWidth: 1, borderStyle: 'dashed', borderColor: C.line, borderRadius: 12, backgroundColor: C.paper2},
  tip: {backgroundColor: C.ink, borderRadius: 12, padding: 10, marginBottom: 8}, tipValue: {color: '#fff', fontWeight: '700', fontSize: 15}, tipMeta: {color: '#cfd8da', fontSize: 11.5, marginTop: 2}, tipLink: {color: '#9fe3d8', fontSize: 12, fontWeight: '700', marginTop: 6},
  hint: {color: C.muted, fontSize: 11.5, marginBottom: 8},
});
