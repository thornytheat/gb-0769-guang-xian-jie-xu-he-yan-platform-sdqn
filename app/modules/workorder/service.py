import copy
import re

from app.config import MAX_BATCH, MAX_CREW, SPEC, SOURCES, WAVE_KEYS
from app.errors import InspectionError
from app.modules.archive.identity import canonical, digest
from app.modules.workorder import model
from app.modules.workorder.storage import (crew_rows, decode, encode, insert_revision, latest_revision, now,
                                           order_row, reader, revision_at, revision_list, session, summary)

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', re.ASCII)
SOURCE_ORDER = ('a', 'b', 'design')
DEFAULT_LABELS = {'short': '短波', 'long': '长波'}


def _name(value, code, limit, begin=True):
    if not isinstance(value, str):
        raise InspectionError(code, status=400)
    text = value.strip()
    pattern = NAME if begin else re.compile(r'[A-Za-z0-9][A-Za-z0-9._@ :-]{0,63}', re.ASCII)
    if not text or len(text) > limit or not pattern.fullmatch(text):
        raise InspectionError(code, status=400)
    return text


def batch_name(value):
    return _name(value, 'BAD_BATCH_NAME', MAX_BATCH)


def crew_name(value):
    return _name(value, 'BAD_CREW', MAX_CREW, begin=False)


def validate_wave(key, wave):
    """Validate one wavelength's material before it reaches the pipeline."""
    if not isinstance(wave, dict) or set(wave) - {'traces', 'settings', 'label'}:
        raise InspectionError('BAD_WAVE', key, 400)
    traces, settings = wave.get('traces'), wave.get('settings')
    if not isinstance(traces, dict) or set(traces) != set(SOURCES):
        raise InspectionError('WAVE_TRACES_REQUIRED', key, 400)
    if any(not isinstance(text, str) for text in traces.values()):
        raise InspectionError('WAVE_TRACE_TEXT_REQUIRED', key, 400)
    if not isinstance(settings, dict):
        raise InspectionError('WAVE_SETTINGS_REQUIRED', key, 400)
    label = wave.get('label', DEFAULT_LABELS[key])
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > 32:
        raise InspectionError('BAD_WAVE_LABEL', key, 400)
    return {'traces': {source: traces[source] for source in SOURCE_ORDER}, 'settings': settings,
            'label': label.strip()}


def normalize_waves(value):
    if not isinstance(value, dict) or set(value) != set(WAVE_KEYS):
        raise InspectionError('TWO_WAVES_REQUIRED', status=400)
    return {key: validate_wave(key, value[key]) for key in WAVE_KEYS}


def _report(batch, orders, created=(), revised=(), applied=True):
    return {'batch': batch, 'applied': applied, 'created': len(created), 'revised': len(revised),
            'created_orders': list(created), 'revised_orders': list(revised),
            'orders': orders}


def _all_orders(db, batch):
    return [order_row(db, row[0]) for row in db.execute('SELECT id FROM work_orders WHERE batch=? ORDER BY joint', (batch,))]


def _withdraw_document(decision):
    """Turn a non-over evaluation into a revision that closes an existing order.

    The reason stays whatever the new evidence actually showed (未再超限,
    某一波长缺测, 接头消失); only the recommendation becomes WITHDRAW so the
    stored order no longer sends anyone out.
    """
    payload = {'batch': decision.get('batch'), 'joint': decision['joint'], 'recommendation': model.WITHDRAW,
               'reason': decision['reason'], 'waves': decision.get('waves'), 'settings': decision.get('settings'),
               'previous_evidence_sha256': decision['evidence_sha256']}
    text = model.WITHDRAWALS[decision['reason']]
    document = copy.deepcopy(decision)
    document.update({'recommendation': model.WITHDRAW, 'title': text['title'],
                     'rationale': text['rationale'], 'instruction': text['instruction'],
                     'evidence_sha256': digest(canonical(payload))})
    return document


def _sync(db, batch, evaluation, stamp):
    """Bring stored orders in line with one evaluation, revision by revision."""
    current = {order['joint']: order for order in _all_orders(db, batch)}
    seen = set()
    created, revised = [], []
    for decision in evaluation['joints']:
        joint, order = decision['joint'], current.get(decision['joint'])
        seen.add(joint)
        dispatchable = decision['recommendation'] in model.DISPATCHED
        if order is None and not dispatchable:
            continue
        if order is None:
            db.execute('INSERT INTO work_orders VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)',
                       (decision['order_id'], batch, joint, 1, stamp if dispatchable else None, stamp))
            insert_revision(db, decision['order_id'], 1, decision, dispatchable, stamp)
            created.append(decision['order_id'])
            continue
        latest = latest_revision(db, order['order_id'])
        document = decision if dispatchable else _withdraw_document({**decision, 'batch': batch})
        if latest['evidence_sha256'] == document['evidence_sha256']:
            continue
        if not dispatchable and latest['recommendation'] == model.WITHDRAW:
            continue
        revision = latest['revision'] + 1
        insert_revision(db, order['order_id'], revision, document, 0, stamp)
        db.execute('UPDATE work_orders SET current_revision=? WHERE id=?', (revision, order['order_id']))
        revised.append(order['order_id'])
    for joint, order in current.items():
        if joint in seen:
            continue
        latest = latest_revision(db, order['order_id'])
        if latest['recommendation'] == model.WITHDRAW:
            continue
        document = model.absent(batch, joint, latest['evidence_sha256'])
        revision = latest['revision'] + 1
        insert_revision(db, order['order_id'], revision, document, 0, stamp)
        db.execute('UPDATE work_orders SET current_revision=? WHERE id=?', (revision, order['order_id']))
        revised.append(order['order_id'])
    return created, revised


def _details(db, batch):
    return [_summary(db, order) for order in _all_orders(db, batch)]


def preview(batch, waves):
    batch = batch_name(batch)
    return model.evaluate(batch, normalize_waves(waves))


def _appended(db, batch, wave):
    """Rows appended to one wavelength, keyed by source, in submission order."""
    rows = {source: [] for source in SOURCE_ORDER}
    for source, line in db.execute('SELECT source, line FROM work_order_measurements WHERE batch=? AND wave=? '
                                   'ORDER BY seq', (batch, wave)):
        rows[source].append(line)
    return rows


def _effective(waves, db, batch):
    """Base waves plus every late measurement appended so far.

    The batch row stores only the *base* material; appended rows live in their
    own table and are re-applied here on each evaluation.  Those appended rows
    are therefore never duplicated, and re-dispatching the original sample (the
    console re-posts it) cannot erase a late measurement.
    """
    material = copy.deepcopy(waves)
    for key in WAVE_KEYS:
        for source, lines in _appended(db, batch, key).items():
            if not lines:
                continue
            current = material[key]['traces'][source]
            if current and not current.endswith('\n'):
                current += '\n'
            material[key]['traces'][source] = current + '\n'.join(lines) + '\n'
    return material


def dispatch(database, batch, waves):
    """Create or revise work orders for one batch; identical re-runs are no-ops."""
    batch = batch_name(batch)
    waves = normalize_waves(waves)
    with session(database) as db:
        evaluation = model.evaluate(batch, _effective(waves, db, batch))
        stamp = now()
        db.execute('INSERT INTO work_order_batches VALUES (?, ?, ?) ON CONFLICT(batch) DO UPDATE SET waves=excluded.waves, updated_at=excluded.updated_at',
                   (batch, encode(waves), stamp))
        created, revised = _sync(db, batch, evaluation, stamp)
        return _report(batch, _details(db, batch), created, revised)


def _batch_waves(db, batch):
    row = db.execute('SELECT waves FROM work_order_batches WHERE batch=?', (batch,)).fetchone()
    if row is None:
        raise InspectionError('BATCH_NOT_FOUND', status=404)
    return decode(row[0])


def _append_rows(db, batch, wave, ingest_id, rows, stamp):
    seq = db.execute('SELECT COALESCE(MAX(seq), 0) FROM work_order_measurements WHERE batch=?', (batch,)).fetchone()[0]
    for source in SOURCE_ORDER:
        for line in (rows or {}).get(source, []):
            seq += 1
            db.execute('INSERT INTO work_order_measurements VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (batch, seq, ingest_id, wave, source, line, stamp))


def _row_lines(value, source):
    if not isinstance(value, list) or any(not isinstance(line, str) or '\n' in line or '\r' in line for line in value):
        raise InspectionError('BAD_MEASUREMENT_ROWS', source, 400)
    return [line for line in value if line.strip()]


def apply_measurement(database, batch, record):
    """Merge a late measurement into a stored batch and revise affected orders.

    The ingest id is derived from the record's content, so retrying the exact
    same submission neither appends rows twice nor opens a second work order.
    """
    batch = batch_name(batch)
    if not isinstance(record, dict):
        raise InspectionError('MEASUREMENT_OBJECT_REQUIRED', status=400)
    if set(record) - {'wave', 'rows', 'traces', 'settings', 'note'}:
        raise InspectionError('BAD_MEASUREMENT', status=400)
    wave = record.get('wave')
    if wave not in WAVE_KEYS:
        raise InspectionError('BAD_WAVE', status=400)
    rows, traces, settings = record.get('rows'), record.get('traces'), record.get('settings')
    if rows is None and traces is None and settings is None:
        raise InspectionError('EMPTY_MEASUREMENT', status=400)
    if rows is not None:
        if not isinstance(rows, dict) or set(rows) - set(SOURCES):
            raise InspectionError('BAD_MEASUREMENT_ROWS', wave, 400)
        rows = {source: sorted(_row_lines(value, source)) for source, value in rows.items()}
    if traces is not None:
        if not isinstance(traces, dict) or set(traces) != set(SOURCES):
            raise InspectionError('WAVE_TRACES_REQUIRED', wave, 400)
        if any(not isinstance(text, str) for text in traces.values()):
            raise InspectionError('WAVE_TRACE_TEXT_REQUIRED', wave, 400)
    if settings is not None and not isinstance(settings, dict):
        raise InspectionError('WAVE_SETTINGS_REQUIRED', wave, 400)
    note = record.get('note')
    if note is not None and (not isinstance(note, str) or len(note) > 120):
        raise InspectionError('BAD_MEASUREMENT_NOTE', status=400)
    ingest_id = digest(canonical([SPEC, 'measurement', batch, wave, rows, traces, settings]))
    with session(database) as db:
        existing = db.execute('SELECT document FROM work_order_ingests WHERE batch=? AND ingest_id=?',
                              (batch, ingest_id)).fetchone()
        if existing is not None:
            # A retry of the exact same submission: report this call as a no-op
            # while still returning the batch so the caller sees the real state.
            prior = decode(existing[0])
            return {'batch': batch, 'wave': prior['wave'], 'ingest_id': ingest_id, 'applied': False,
                    'created': 0, 'revised': 0, 'created_orders': [], 'revised_orders': [],
                    'orders': _details(db, batch)}
        waves = _batch_waves(db, batch)
        material = copy.deepcopy(waves)
        if traces is not None:
            material['traces'] = {source: traces[source] for source in SOURCE_ORDER}
        if settings is not None:
            material['settings'] = settings
        waves = normalize_waves(material)
        stamp = now()
        _append_rows(db, batch, wave, ingest_id, rows, stamp)
        evaluation = model.evaluate(batch, _effective(waves, db, batch))
        db.execute('UPDATE work_order_batches SET waves=?, updated_at=? WHERE batch=?', (encode(waves), stamp, batch))
        created, revised = _sync(db, batch, evaluation, stamp)
        report = {'batch': batch, 'wave': wave, 'ingest_id': ingest_id, 'applied': True,
                  'created': len(created), 'revised': len(revised), 'created_orders': created,
                  'revised_orders': revised, 'orders': _details(db, batch)}
        db.execute('INSERT INTO work_order_ingests VALUES (?, ?, ?, ?)',
                   (batch, ingest_id, encode(report), stamp))
        return report


def _batch_detail(db, batch):
    row = db.execute('SELECT waves, updated_at FROM work_order_batches WHERE batch=?', (batch,)).fetchone()
    if row is None:
        raise InspectionError('BATCH_NOT_FOUND', status=404)
    return decode(row[0]), row[1]


def batch(database, batch):
    """Current stored waves and orders of one batch, for reviewing a revision."""
    batch = batch_name(batch)
    with reader(database) as db:
        waves, updated = _batch_detail(db, batch)
        return {'batch': batch, 'updated_at': updated, 'waves': waves, 'orders': _details(db, batch),
                'measurements': {key: _appended(db, batch, key) for key in WAVE_KEYS}}


def effective_waves(database, batch):
    """Stored base material with every late measurement folded in, or None."""
    batch = batch_name(batch)
    with reader(database) as db:
        try:
            return _effective(_batch_waves(db, batch), db, batch)
        except InspectionError:
            return None


def orders(database, batch=None):
    with reader(database) as db:
        if batch is None:
            rows = [order_row(db, row[0]) for row in db.execute('SELECT id FROM work_orders ORDER BY created_at DESC, id LIMIT 200')]
        else:
            rows = _all_orders(db, batch_name(batch))
        return [_summary(db, order) for order in rows]


def _summary(db, order):
    current = latest_revision(db, order['order_id'])
    document = {'summary': summary(order, current), 'current': current}
    if order['claimed_revision'] is not None:
        claim = revision_at(db, order['order_id'], order['claimed_revision'])
        document['claim'] = {'crew': order['claimed_by'], 'claimed_at': order['claimed_at'],
                             'revision': claim['revision'], 'document': claim['document']}
    else:
        document['claim'] = None
    document['claims'] = crew_rows(db, order['order_id'])
    document['revisions'] = revision_list(db, order['order_id'])
    return document


def order(database, order_id):
    with reader(database) as db:
        row = order_row(db, order_id)
        if row is None:
            raise InspectionError('WORK_ORDER_NOT_FOUND', status=404)
        return _summary(db, row)


def claim(database, order_id, crew):
    crew = crew_name(crew)
    with session(database) as db:
        row = order_row(db, order_id)
        if row is None:
            raise InspectionError('WORK_ORDER_NOT_FOUND', status=404)
        current = latest_revision(db, order_id)
        if current['recommendation'] not in model.DISPATCHED:
            raise InspectionError('WORK_ORDER_WITHDRAWN', status=409)
        if row['claimed_by'] is not None:
            # An order already out with a crew is not re-dispatched, even if a
            # later revision changed the advice: that crew keeps the reference
            # basis and sees the change on the order.
            if row['claimed_by'] != crew:
                raise InspectionError('WORK_ORDER_ALREADY_CLAIMED', status=409)
            return {**_summary(db, order_row(db, order_id)), 'created': False}
        stamp = now()
        db.execute('INSERT INTO work_order_claims VALUES (?, ?, ?, ?) ON CONFLICT(order_id, revision, crew) DO NOTHING',
                   (order_id, current['revision'], crew, stamp))
        db.execute('UPDATE work_orders SET claimed_revision=?, claimed_by=?, claimed_at=? WHERE id=?',
                   (current['revision'], crew, stamp, order_id))
        return {**_summary(db, order_row(db, order_id)), 'created': True}
