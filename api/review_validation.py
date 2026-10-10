"""Field-level validation for "Confirm and save to history" in the document extraction review.

Only rows the caregiver explicitly INCLUDED become structured history, and only they are validated. Excluded rows,
removed rows, empty auto-created rows and the raw transcription text never block confirmation. Safety stays strict:
an included row must be checked against the original, unreadable or placeholder values are never accepted, dates must
be real ISO dates, and nothing is inferred (an undated vaccine row is not treated as given).

validate_review() returns a list of errors; each error names the row (index, kind, per-kind row number, source page),
the field, a machine reason and a human message, so the UI can highlight the exact field and jump to its page.
"""
import re
from datetime import date
from pydantic import ValidationError

KIND_LABEL = {'medicine': 'Medicine', 'vaccine': 'Vaccine', 'observation': 'Observation', 'episode': 'Episode'}
FIELD_LABEL = {
    'name': 'Name', 'dose_label': 'Dose number', 'given_date': 'Date given', 'due_date': 'Appointment date',
    'next_due_date': 'Next dose date', 'next_dose_label': 'Next dose label', 'batch_number': 'Batch number',
    'facility': 'Clinic / hospital', 'doctor': 'Doctor', 'reminder_days': 'Reminder days', 'note': 'Remarks',
    'instructions': 'Directions', 'generic_name': 'Generic name', 'brand_name': 'Brand', 'dosage': 'Dose',
    'dose_unit': 'Dose unit', 'route': 'Route', 'frequency': 'Frequency', 'prescription_date': 'Prescription date',
    'start_date': 'Planned start', 'end_date': 'Planned end', 'prescribing_doctor': 'Prescriber', 'indication': 'Indication',
    'test_name': 'Test', 'value': 'Result', 'unit': 'Unit', 'specimen': 'Specimen', 'record_date': 'Result date',
    'reference_range': 'Reference interval', 'organism': 'Organism', 'culture_findings': 'Culture findings',
    'susceptibility': 'Susceptibility', 'title': 'Title', 'started_on': 'Start date', 'ended_on': 'End date',
    'symptoms': 'Symptoms', 'documented_diagnosis': 'Diagnosis', 'notes': 'Notes', 'page': 'Source page',
}
# Fields whose content makes a row meaningful; a row with none of them filled is an empty placeholder and is skipped.
KEY_FIELDS = {
    'medicine': ('name', 'instructions', 'dosage', 'frequency', 'generic_name', 'brand_name'),
    'vaccine': ('name', 'given_date', 'due_date', 'next_due_date', 'dose_label', 'batch_number'),
    'observation': ('test_name', 'value', 'record_date', 'organism', 'culture_findings'),
    'episode': ('title', 'started_on', 'documented_diagnosis', 'symptoms'),
}
REQUIRED = {'medicine': ('name',), 'vaccine': ('name',), 'observation': ('test_name', 'value', 'record_date'), 'episode': ('title', 'started_on')}
DATE_FIELDS = {'given_date', 'due_date', 'next_due_date', 'prescription_date', 'start_date', 'end_date', 'record_date', 'started_on', 'ended_on'}
# Placeholders people and OCR use for text that could not be read. Never accepted as a value in structured history.
UNREADABLE = re.compile(r'\?|\[\s*unreadable\s*\]|\billegible\b|\bunreadable\b|\bunclear\b|_{3,}|…|\.\.\.', re.I)
FREE_TEXT = {'note', 'notes', 'instructions', 'indication', 'symptoms', 'documented_diagnosis', 'culture_findings', 'susceptibility', 'reference_range'}


def blank(v):
    return v is None or (isinstance(v, str) and not v.strip())


def is_empty(kind, fields):
    return all(blank(fields.get(k)) for k in KEY_FIELDS.get(kind, ()))


def row_numbers(items):
    """Per-kind 1-based row numbers matching the review UI ("Medicine row 2")."""
    seen, out = {}, []
    for it in items:
        seen[it.kind] = seen.get(it.kind, 0) + 1
        out.append(seen[it.kind])
    return out


def _err(i, item, n, field, reason, message):
    where = f"Page {item.page} · {KIND_LABEL[item.kind]} row {n}"
    return {'index': i, 'page': item.page, 'entry_type': item.kind, 'row_number': n, 'field': field,
            'field_label': FIELD_LABEL.get(field, field) if field else None, 'reason': reason,
            'message': f"{where}: {message}"}


def included_rows(items):
    """Rows that will become history: explicitly included and not an empty placeholder."""
    return [(i, it) for i, it in enumerate(items) if it.included and not is_empty(it.kind, it.fields)]


def validate_review(body, page_count, models):
    """Return (errors, rows). `models` maps kind -> pydantic input model used for the record."""
    errors = []
    numbers = row_numbers(body.items)
    rows = included_rows(body.items)
    for i, item in rows:
        n = numbers[i]
        f = item.fields
        e = lambda field, reason, message: errors.append(_err(i, item, n, field, reason, message))
        if not 1 <= item.page <= max(1, page_count):
            e('page', 'page_out_of_range', f'Source page must be between 1 and {page_count}.')
        if not item.verified:
            e(None, 'unverified', 'Tick "Include in history" only after checking this row against the original, or exclude it.')
        for k in REQUIRED[item.kind]:
            if blank(f.get(k)):
                e(k, 'required', f'{FIELD_LABEL.get(k, k)} is required. Enter it from the original or exclude this row.')
        for k, v in f.items():
            if not isinstance(v, str) or blank(v):
                continue
            if k in DATE_FIELDS:
                try:
                    date.fromisoformat(v.strip())
                except ValueError:
                    e(k, 'invalid_date', f'{FIELD_LABEL.get(k, k)} "{v}" is not a complete date. Confirm it as YYYY-MM-DD or leave it blank.')
            elif k not in FREE_TEXT and UNREADABLE.search(v):
                e(k, 'unreadable', f'{FIELD_LABEL.get(k, k)} is marked unreadable ("{v}"). Correct it from the original, leave it blank, or exclude this row.')
        if item.kind == 'vaccine' and all(blank(f.get(k)) for k in ('given_date', 'due_date', 'next_due_date')):
            e('given_date', 'date_required', 'Enter the date given or the appointment date shown on the card. A dose is never assumed to be given; exclude the row if the card shows no date.')
        if item.kind == 'medicine' and item.activate_reminders:
            e(None, 'reminders_not_allowed', 'Reminders for a prescription are activated separately after it is saved.')
        if any(x['index'] == i for x in errors):
            continue  # schema errors would only repeat the problems above
        try:
            data = dict(f, source_page=item.page)
            if item.kind == 'observation':
                data['verified'] = True
            if item.kind == 'vaccine':
                data.update(source_type='vaccination_card', duplicate_confirmed=item.duplicate_confirmed)
            models[item.kind].model_validate({k: v for k, v in data.items() if k not in ('next_due_date', 'next_dose_label')})
        except ValidationError as exc:
            for x in exc.errors():
                k = str(x['loc'][0]) if x['loc'] else None
                e(k, 'invalid', f"{FIELD_LABEL.get(k, k) if k else 'Row'}: {x['msg']}.")
    if not body.checked_against_original:
        errors.append({'index': None, 'page': None, 'entry_type': None, 'row_number': None, 'field': 'checked_against_original',
                       'field_label': 'Final acknowledgement', 'reason': 'acknowledgement_missing',
                       'message': 'Tick "I verified all included entries. No unreadable field has been guessed." to confirm.'})
    return errors, rows


def summary(errors):
    n = len(errors)
    return f"{n} item{'s' if n != 1 else ''} need{'s' if n == 1 else ''} review before this document can be confirmed."
