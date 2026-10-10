"""Source-labelled exports of recorded facts, without clinical interpretation."""
from datetime import datetime
from zoneinfo import ZoneInfo
from .models import (Document,Medicine,MedicationEvent,HospitalVisit,Vaccine,Observation,
    Admission,TreatmentAdministration,Episode,Growth)


def provenance(row):
    return f'document #{row.source_document_id}, page {row.source_page}' if getattr(row,'source_document_id',None) else 'no source document attached; caregiver-entered'


def medical_summary(db,p):
    lines=['BABYCARE AI — FAMILY-REPORTED MEDICAL HISTORY',f'Child: {p.name} | DOB: {p.dob} | Timezone: {p.tz}',
        f'Generated: {datetime.now(ZoneInfo(p.tz)).strftime("%Y-%m-%d %H:%M")}',
        'Not a clinician-certified medical record. Verify against source documents.','', 'MEDICATION COURSES']
    for m in db.query(Medicine).filter_by(patient_id=p.id).order_by(Medicine.start_date):
        lines.append(f'- Course #{m.id}: {m.name}; type {m.course_type.replace("_"," ")}; generic {m.generic_name or "unknown"}; brand {m.brand_name or "unknown"}; {m.status}; prescription date {m.prescription_date or "unknown"}')
        lines.append(f'  Planned {m.start_date or "start unknown"} to {m.end_date or "end unknown"}; actual course dates {m.actual_start_date or "start unknown"} to {m.actual_stop_date or "stop unknown"}; {provenance(m)}')
        lines.append(f'  Recorded instructions: {m.instructions}; recorded dose {m.dosage} {m.dose_unit}; route {m.route or "not documented"}; frequency {m.frequency or "not documented"}; prescriber {m.prescribing_doctor or "not documented"}')
        events=db.query(MedicationEvent).filter_by(medicine_id=m.id).order_by(MedicationEvent.occurred_at).all()
        if not any(e.event_type=='given' for e in events):lines.append('  No actual given events recorded. A prescription does not establish administration or continuous dosing.')
        for e in events:lines.append(f'  Event #{e.id}: {e.event_type}; local clock {e.occurred_at}; prescription version {e.medicine_version}; {e.source}; {e.note}; {provenance(e)}')
    lines+=['','VACCINATION RECORDS — verified transcription is not independent clinical validation']
    for v in db.query(Vaccine).filter_by(patient_id=p.id).order_by(Vaccine.given_date,Vaccine.due_date):
        state='Reference only' if v.source_type=='India_UIP_reference' else 'Verified administration record' if v.confirmed and v.given_date else 'Confirmed appointment' if v.confirmed else 'UNVERIFIED transcription'
        lines.append(f'- #{v.id}: {v.name} {v.dose_label}; {state}; appointment {v.due_date or "unknown"}; recorded given {v.given_date or "not recorded"}; schedule {v.schedule}; {provenance(v)}; batch {v.batch_number or "not recorded"}; clinic {v.facility or "not recorded"}')
    lines+=['','HOSPITAL ADMISSIONS']
    for a in db.query(Admission).filter_by(patient_id=p.id).order_by(Admission.admitted_on):
        lines.append(f'- #{a.id}: {a.admitted_on} to {a.discharged_on or "discharge unknown"}; {a.facility}; doctor {a.doctor}; documented diagnosis: {a.diagnosis or "not documented"}; recorded plan: {a.treatment_plan}; {provenance(a)}; discharge document #{a.discharge_document_id or "not attached"}')
    lines+=['','HOSPITAL APPOINTMENTS (attended does NOT mean treatment administered)']
    for v in db.query(HospitalVisit).filter_by(patient_id=p.id).order_by(HospitalVisit.planned_at):
        lines.append(f'- Visit #{v.id}: local clock {v.planned_at}; {v.facility}; {v.purpose}; {v.status}; confirmed={v.confirmed}; admission #{v.admission_id or "not linked"}; {provenance(v)}')
    lines+=['','DOCUMENTED HOSPITAL ADMINISTRATIONS — copied from the attached administration record']
    for t in db.query(TreatmentAdministration).filter_by(patient_id=p.id).order_by(TreatmentAdministration.occurred_at):
        lines.append(f'- #{t.id}: {t.occurred_at} UTC; visit #{t.visit_id}; {t.medication}; recorded dose {t.recorded_dose}; recorded route {t.recorded_route}; {provenance(t)}')
    lines+=['','LABORATORY RESULTS — original units and laboratory-specific reference intervals']
    for o in db.query(Observation).filter_by(patient_id=p.id).order_by(Observation.record_date):
        lines.append(f'- #{o.id}: {o.record_date}; {o.test_name}: {o.value} {o.unit}; specimen {o.specimen or "unknown"}; printed reference interval {o.reference_range or "not recorded"}; transcription verified={o.verified}; {provenance(o)}')
        if o.organism or o.culture_findings or o.susceptibility:lines.append(f'  Reported organism: {o.organism or "unknown"}; culture findings: {o.culture_findings or "unknown"}; susceptibility: {o.susceptibility or "not documented"}')
    lines+=['No adult reference intervals, diagnostic interpretations, or inferred antibiotic susceptibility are added.','', 'ILLNESS EPISODES — associations entered by caregivers']
    for e in db.query(Episode).filter_by(patient_id=p.id).order_by(Episode.started_on):
        lines.append(f'- #{e.id}: {e.title}; {e.started_on} to {e.ended_on or "end unknown"}; symptoms: {e.symptoms}; documented diagnosis: {e.documented_diagnosis or "not documented"}; {provenance(e)}; linked records: {e.links_json}; {e.notes}')
    lines+=['','GROWTH & DEVELOPMENT — recorded measurements, no percentile interpretation']
    for g in db.query(Growth).filter_by(patient_id=p.id).order_by(Growth.record_date):
        lines.append(f'- {g.record_date}: weight {g.weight_kg or "unknown"} kg; length {g.length_cm or "unknown"} cm; head circumference {g.head_cm or "unknown"} cm; milestone: {g.milestone}; {g.note}; {provenance(g)}')
    lines+=['','SOURCE DOCUMENT INDEX']
    for d in db.query(Document).filter_by(patient_id=p.id):lines.append(f'- Document #{d.id}{" ["+d.source_ref+"]" if d.source_ref else ""}: {d.title}; {d.category}; recorded date {d.record_date or "unknown"}; {d.page_count} page(s); {d.extraction_status}{"; "+d.notes if d.notes else ""}')
    lines+=['','Original reports and treating clinician remain authoritative. This summary does not establish a diagnosis, reinfection, treatment efficacy, or a catch-up schedule.']
    return '\n'.join(lines)


def additional_timeline(db,pid):
    entries=[]
    for model,dt,kind,title,note in [
        (Admission,'admitted_on','admission','facility','diagnosis'),
        (Episode,'started_on','illness','title','documented_diagnosis'),
        (Growth,'record_date','growth','milestone','note'),
        (Observation,'record_date','laboratory','test_name','value'),
        (TreatmentAdministration,'occurred_at','documented_administration','medication','recorded_dose')]:
        for row in db.query(model).filter_by(patient_id=pid):
            entries.append({'date':getattr(row,dt).isoformat()[:10],'kind':kind,'title':getattr(row,title) or kind.replace('_',' ').title(), 'note':getattr(row,note),'source_document_id':row.source_document_id,'source_page':row.source_page,'record_id':row.id})
    for event,med in db.query(MedicationEvent,Medicine).join(Medicine).filter(Medicine.patient_id==pid):
        entries.append({'date':event.occurred_at.date().isoformat(),'kind':'medication_'+event.event_type,'title':med.name,'note':f'{event.source}; {event.note}', 'source_document_id':event.source_document_id,'source_page':event.source_page,'record_id':event.id})
    return entries
