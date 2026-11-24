from flask import Blueprint, abort, current_app, render_template, request

from .config import SOURCES, SPEC
from .errors import InspectionError
from .input import sample
from .input_dual import dual
from .modules.archive.service import archive
from .modules.archive.storage import get, recent
from .modules.certificate.service import certify
from .modules.workorder import service as workorder_service
from .pipeline import run
from .modules.calibration.contract import FIELDS
from .views import CODES, LABELS, half_mm_metres, metres, trace_frames

console = Blueprint('console', __name__)
console.add_app_template_filter(metres, 'metres')
console.add_app_template_filter(half_mm_metres, 'half_mm_metres')

WAVES = (('short', '短波'), ('long', '长波'))


def material(mode):
    if mode == 'custom':
        return {'traces':{key:request.form.get(key,'') for key in SOURCES},
                'settings':{key:request.form.get(key,'') for key in FIELDS}}
    return sample(mode)


def dual_material(mode):
    """Two wavelengths, each with its own traces, settings and label."""
    if mode == 'custom':
        settings = {key:request.form.get(key,'') for key in FIELDS}
        waves = {}
        for key, label in WAVES:
            waves[key] = {'traces': {source: request.form.get(f'{key}_{source}','') for source in SOURCES},
                          'settings': settings, 'label': label}
        return waves
    return dual()


def merged_dual(waves):
    """Flatten the two waves for the edit form (shared calibration fields)."""
    first = next(iter(waves.values()), {})
    return {'settings': first.get('settings') or {},
            'traces': {key: value['traces'] for key, value in waves.items()}}


def workorder_view(database, mode):
    state = {'evaluation': None, 'orders': [], 'issued': None, 'claim_result': None, 'measure_result': None,
             'focus_order': None, 'waves': [key for key, _ in WAVES], 'batch': '', 'materials': {},
             'stored': {}}
    batch = (request.values.get('batch') or 'B1').strip() or 'B1'
    state['batch'] = batch
    error = None
    try:
        waves = dual_material(mode)
        state['materials'] = merged_dual(waves)
        # Once a batch exists, its stored material plus late measurements is
        # authoritative; only preview from the form before it is first dispatched.
        stored = workorder_service.effective_waves(database, batch)
        state['evaluation'] = workorder_service.preview(batch, stored or waves)
        state['orders'] = workorder_service.orders(database, batch)
    except InspectionError as exc:
        waves = None
        error = exc.code
    state['stored'] = {order['summary']['order_id']: order['summary'] for order in state['orders']}
    action = request.form.get('action')
    if waves is not None:
        try:
            if action == 'claim':
                result = workorder_service.claim(database, request.form.get('order',''), request.form.get('crew',''))
                fresh = result.pop('created')
                state['focus_order'] = request.form.get('order','')
                if fresh:
                    state['claim_result'] = f"已领取 {result['summary']['joint']}（{result['claim']['crew']}）"
                elif result['claim']:
                    state['claim_result'] = f"已由 {result['claim']['crew']} 领取，未重复派发"
                _refresh(state, database, batch, waves)
            elif action == 'measure':
                lines = [line.strip() for line in request.form.get('rows','').replace('\r','').split('\n') if line.strip()]
                report = workorder_service.apply_measurement(database, batch, {
                    'wave': request.form.get('wave',''), 'rows': {request.form.get('source',''): lines}})
                state['measure_result'] = (f"已补录，新建 {report['created']} 张 / 修订 {report['revised']} 张"
                                           if report['applied'] else '重复提交，未重复派发')
                _refresh(state, database, batch, waves)
            elif request.method == 'POST':
                state['issued'] = workorder_service.dispatch(database, batch, waves)
                _refresh(state, database, batch, waves)
        except InspectionError as exc:
            state['claim_result'] = exc.code
            state['measure_result'] = state['measure_result'] or exc.code
    return state, error


def _refresh(state, database, batch, fallback):
    """Reload stored orders and re-evaluate from the batch, late rows included."""
    state['orders'] = workorder_service.orders(database, batch)
    state['stored'] = {order['summary']['order_id']: order['summary'] for order in state['orders']}
    stored = workorder_service.effective_waves(database, batch)
    state['evaluation'] = workorder_service.preview(batch, stored or fallback)


@console.route('/', methods=['GET','POST'])
@console.route('/inspect/<section>', methods=['GET','POST'])
def view(section='design'):
    if section not in LABELS:
        abort(404)
    mode = request.values.get('sample','dirty')
    database = current_app.config['DATABASE']
    evidence, frames, entries, data = None, {}, [], {}
    issued, fresh, error, sources = None, False, None, {}
    extra = {}
    if section == 'workorder':
        extra, error = workorder_view(database, mode)
        sources = extra.pop('materials')
        issued = extra.pop('issued')
    else:
        try:
            sources = material(mode)
            if section in ('archive','certificate'):
                if section == 'archive' and request.method == 'POST':
                    issued, fresh = archive(database, sources)
                entries = recent(database)
                identifier = request.args.get('inspection') or (entries[0]['inspection_id'] if entries else None)
                if section == 'certificate' and identifier:
                    evidence = certify(get(database, identifier))
            else:
                data = run(sources, section)
                if 'calibration' in data:
                    frames = trace_frames(data['calibration'])
        except InspectionError as exc:
            error = exc.code
    return render_template('console.html', section=section, labels=LABELS, codes=CODES, spec=SPEC, mode=mode,
                           data=data, frames=frames, error=error, materials=sources, entries=entries,
                           evidence=evidence, issued=issued, fresh=fresh, fields=FIELDS, **extra)
