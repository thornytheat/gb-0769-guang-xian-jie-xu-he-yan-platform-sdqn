from flask import Blueprint, current_app, request

from .errors import InspectionError
from .modules.workorder import service

workorders = Blueprint('fiber_workorders', __name__, url_prefix='/api/workorders')


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise InspectionError('JSON_OBJECT_REQUIRED', status=400)
    return body


def _database():
    return current_app.config['DATABASE']


def _waves(body):
    if set(body) - {'batch', 'waves'}:
        raise InspectionError('BAD_WORK_ORDER_REQUEST', status=400)
    if 'waves' not in body:
        raise InspectionError('TWO_WAVES_REQUIRED', status=400)
    return body.get('batch'), body['waves']


@workorders.post('/preview')
def preview():
    batch, waves = _waves(_body())
    return service.preview(batch, waves)


@workorders.post('')
def dispatch():
    batch, waves = _waves(_body())
    report = service.dispatch(_database(), batch, waves)
    status = 201 if report['created'] else 200
    return report, status


@workorders.get('')
def listing():
    return {'orders': service.orders(_database(), request.args.get('batch'))}


@workorders.get('/batch/<batch>')
def batch(batch):
    return service.batch(_database(), batch)


@workorders.get('/<identifier>')
def detail(identifier):
    return service.order(_database(), identifier)


@workorders.post('/<identifier>/claim')
def claim(identifier):
    body = _body()
    if set(body) != {'crew'}:
        raise InspectionError('CREW_REQUIRED', status=400)
    result = service.claim(_database(), identifier, body['crew'])
    return result, 201 if result.pop('created') else 200


@workorders.post('/<batch>/measurements')
def measurement(batch):
    return service.apply_measurement(_database(), batch, _body()), 201
