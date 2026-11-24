import json

import pytest

from app.config import DATA, WAVE_KEYS
from app.main import create_app
from app.modules.workorder import model


def waves(folder='dual'):
    base = DATA / folder
    settings = json.loads((base / 'link.json').read_text(encoding='utf-8'))
    return {key: {'traces': {source: (base / key / f'{source}.csv').read_text(encoding='utf-8')
                             for source in ('a', 'b', 'design')},
                  'settings': settings, 'label': {'short': '短波', 'long': '长波'}[key]} for key in WAVE_KEYS}


def dispatch(client, batch='B1', material=None):
    return client.post('/api/workorders', json={'batch': batch, 'waves': material or waves()})


def by_joint(report):
    return {order['summary']['joint']: order for order in report['orders']}


def recommendation(report, joint):
    return by_joint(report)[joint]['current']['document']['recommendation']


def test_two_over_limit_wavelengths_refuse_joint_and_long_only_checks_bend(client):
    report = dispatch(client).json
    assert recommendation(report, 'F-01') == model.REFUSE_JOINT
    assert recommendation(report, 'F-02') == model.CHECK_BEND


def test_long_only_instruction_forbids_remelt(client):
    joint = by_joint(dispatch(client).json)['F-02']['current']['document']
    assert '弯折' in joint['instruction'] and '禁止重熔' in joint['instruction']
    assert joint['waves']['short']['state'] == model.PASS
    assert joint['waves']['long']['state'] == model.OVER


def test_wavelength_missing_one_end_is_unmeasured_not_over(client):
    # Long-wave F-03 has only one end; short-wave F-03 is over.  A half-measured
    # wavelength may not be counted either way, and re-melting is held back.
    report = dispatch(client).json
    joint = by_joint(report)['F-03']['current']['document']
    assert joint['waves']['long']['state'] == model.MISSING
    assert joint['waves']['long']['code'] == 'SINGLE_SIDE_ONLY'
    assert joint['recommendation'] == model.COMPLETE_MEASUREMENT
    assert '禁止重熔' in joint['instruction']


def test_clean_joint_is_not_dispatched(client):
    report = dispatch(client).json
    assert 'F-04' not in by_joint(report)
    assert report['created'] == 3


def test_classification_matrix():
    assert model.classify(model.OVER, model.OVER) == (model.REFUSE_JOINT, 'BOTH_WAVES_OVER')
    assert model.classify(model.PASS, model.OVER) == (model.CHECK_BEND, 'LONG_WAVE_ONLY')
    assert model.classify(model.OVER, model.PASS) == (model.REFUSE_JOINT, 'SHORT_WAVE_ONLY')
    assert model.classify(model.OVER, model.MISSING) == (model.COMPLETE_MEASUREMENT, 'OTHER_WAVE_UNMEASURED')
    assert model.classify(model.MISSING, model.OVER) == (model.COMPLETE_MEASUREMENT, 'OTHER_WAVE_UNMEASURED')
    assert model.classify(model.PASS, model.PASS) == (model.NONE, 'NO_OVER_LIMIT')


def test_rerun_does_not_dispatch_twice(client):
    first = dispatch(client)
    again = dispatch(client)
    assert (first.status_code, again.status_code) == (201, 200)
    assert (again.json['created'], again.json['revised']) == (0, 0)
    assert len(client.get('/api/workorders').json['orders']) == len(first.json['orders'])


def test_late_measurement_revises_order_and_keeps_dispatched_orders_single(client):
    report = dispatch(client).json
    order_id = by_joint(report)['F-03']['summary']['order_id']
    late = client.post('/api/workorders/B1/measurements',
                       json={'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}, 'note': 'late'})
    assert late.status_code == 201 and late.json['applied'] is True
    # F-03 now has both long-wave ends passing and short wave over: a joint defect.
    assert recommendation(late.json, 'F-03') == model.REFUSE_JOINT
    detail = client.get('/api/workorders/' + order_id).json
    assert [r['revision'] for r in detail['revisions']] == [1, 2]
    assert detail['revisions'][0]['recommendation'] == model.COMPLETE_MEASUREMENT
    assert detail['current']['document']['recommendation'] == model.REFUSE_JOINT
    # Only the affected order is revised; nothing new is dispatched per retry.
    assert late.json['created'] == 0 and late.json['revised'] == 1


def test_retried_measurement_is_idempotent(client):
    dispatch(client)
    body = {'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}}
    first = client.post('/api/workorders/B1/measurements', json=body)
    retry = client.post('/api/workorders/B1/measurements', json=body)
    assert first.json['applied'] is True
    assert retry.status_code == 201 and retry.json['applied'] is False
    assert retry.json['revised'] == 0 and retry.json['created'] == 0
    assert len(client.get('/api/workorders?batch=B1').json['orders']) == 3
    stored = client.get('/api/workorders/batch/B1').json
    assert stored['measurements']['long']['b'] == ['L-B5,540,m,0.10']


def test_late_rows_are_not_duplicated_by_redispatch(client):
    material = waves()
    dispatch(client, material=material)
    client.post('/api/workorders/B1/measurements', json={'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}})
    again = dispatch(client, material=material).json
    assert again['created'] == 0 and again['revised'] == 0
    assert client.get('/api/workorders/batch/B1').json['measurements']['long']['b'] == ['L-B5,540,m,0.10']


def test_parallel_dispatch_creates_each_order_once(client):
    from concurrent.futures import ThreadPoolExecutor
    from app.modules.workorder import service
    database = client.application.config['DATABASE']
    material = waves()
    with ThreadPoolExecutor(max_workers=6) as pool:
        reports = list(pool.map(lambda _: service.dispatch(database, 'B1', material), range(12)))
    assert next(r for r in reports if r['created'])['created'] == 3
    assert all(r['created'] in (0, 3) for r in reports)
    assert len(client.get('/api/workorders?batch=B1').json['orders']) == 3


def test_claim_keeps_original_basis_after_late_measurement(client):
    report = dispatch(client).json
    order_id = by_joint(report)['F-03']['summary']['order_id']
    claimed = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-7'})
    assert claimed.status_code == 201
    assert claimed.json['claim']['revision'] == 1
    original = claimed.json['claim']['document']['recommendation']
    client.post('/api/workorders/B1/measurements',
                json={'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}})
    detail = client.get('/api/workorders/' + order_id).json
    # New recommendation is visible, but the crew's claimed basis is unchanged.
    assert detail['current']['document']['recommendation'] == model.REFUSE_JOINT
    assert detail['claim']['document']['recommendation'] == original
    assert detail['summary']['changed_since_claim'] is True
    assert detail['claims'][0]['crew'] == 'crew-7' and detail['claims'][0]['revision'] == 1


def test_claim_is_idempotent_same_crew_and_conflicts_with_another(client):
    order_id = by_joint(dispatch(client).json)['F-01']['summary']['order_id']
    first = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-1'})
    again = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-1'})
    other = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-2'})
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json['summary']['claimed_by'] == 'crew-1'
    assert (other.status_code, other.json['code']) == (409, 'WORK_ORDER_ALREADY_CLAIMED')


def test_withdrawn_order_stops_dispatch_but_keeps_history(client):
    report = dispatch(client).json
    order_id = by_joint(report)['F-03']['summary']['order_id']
    # Complete the missing long-wave end with a passing reading: short over +
    # long pass is a joint defect, so the order is revised, not duplicated.
    client.post('/api/workorders/B1/measurements', json={'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}})
    # Now replace the short-wave F-03 readings with passing ones: nothing over.
    replaced = waves()
    replaced['short']['traces']['a'] = replaced['short']['traces']['a'].replace('S-A3,600,m,0.05', 'S-A3,600,m,0.01')
    replaced['short']['traces']['b'] = replaced['short']['traces']['b'].replace('S-B3,540,m,0.45', 'S-B3,540,m,0.01')
    client.post('/api/workorders', json={'batch': 'B1', 'waves': replaced})
    detail = client.get('/api/workorders/' + order_id).json
    assert detail['current']['document']['recommendation'] == model.WITHDRAW
    assert detail['current']['document']['reason'] == 'NO_OVER_LIMIT'
    assert detail['revisions'][0]['recommendation'] == model.COMPLETE_MEASUREMENT
    claim = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-9'})
    assert (claim.status_code, claim.json['code']) == (409, 'WORK_ORDER_WITHDRAWN')


def test_claimed_order_is_not_redispatched_to_another_crew_after_revision(client):
    report = dispatch(client).json
    order_id = by_joint(report)['F-03']['summary']['order_id']
    assert client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-1'}).status_code == 201
    client.post('/api/workorders/B1/measurements', json={'wave': 'long', 'rows': {'b': ['L-B5,540,m,0.10']}})
    detail = client.get('/api/workorders/' + order_id).json
    assert detail['current']['revision'] == 2
    assert detail['claim']['revision'] == 1
    assert detail['claims'] == [{'crew': 'crew-1', 'revision': 1, 'claimed_at': detail['claim']['claimed_at']}]
    second = client.post(f'/api/workorders/{order_id}/claim', json={'crew': 'crew-2'})
    assert (second.status_code, second.json['code']) == (409, 'WORK_ORDER_ALREADY_CLAIMED')


def test_removed_joint_is_withdrawn_without_touching_others(client):
    report = dispatch(client).json
    changed = waves()
    for key in WAVE_KEYS:
        changed[key]['traces']['design'] = changed[key]['traces']['design'].replace('F-01,100,m,0.2\n', '')
    after = client.post('/api/workorders', json={'batch': 'B1', 'waves': changed}).json
    # Only the vanished joint gets a withdrawal revision; shifted line numbers
    # elsewhere must not open revisions on unchanged judgements.
    assert after['revised'] == 1 and after['created'] == 0
    removed = client.get('/api/workorders/' + by_joint(report)['F-01']['summary']['order_id']).json
    assert removed['current']['document']['recommendation'] == model.WITHDRAW
    assert removed['current']['document']['reason'] == 'JOINT_ABSENT'
    assert len(removed['revisions']) == 2


def test_dispatch_requires_both_wavelengths_and_names(client):
    assert client.post('/api/workorders', json={'batch': 'B1', 'waves': {'short': waves()['short']}}).json['code'] == 'TWO_WAVES_REQUIRED'
    assert client.post('/api/workorders', json={'waves': waves()}).json['code'] == 'BAD_BATCH_NAME'
    assert client.post('/api/workorders', json={'batch': 'bad name!', 'waves': waves()}).json['code'] == 'BAD_BATCH_NAME'
    bad = waves()
    bad['long']['traces'] = {'a': '', 'b': ''}
    assert client.post('/api/workorders', json={'batch': 'B1', 'waves': bad}).json['code'] == 'WAVE_TRACES_REQUIRED'


def test_missing_order_and_batch_are_404(client):
    assert client.get('/api/workorders/' + '0' * 64).json['code'] == 'WORK_ORDER_NOT_FOUND'
    assert client.get('/api/workorders/batch/never').json['code'] == 'BATCH_NOT_FOUND'


def test_preview_does_not_persist(client):
    preview = client.post('/api/workorders/preview', json={'batch': 'P1', 'waves': waves()})
    assert preview.status_code == 200 and preview.json['counts'][model.CHECK_BEND] == 1
    assert client.get('/api/workorders').json['orders'] == []


def test_batches_are_separate(client):
    dispatch(client, 'B1')
    dispatch(client, 'B2')
    first = {o['summary']['order_id'] for o in client.get('/api/workorders?batch=B1').json['orders']}
    second = {o['summary']['order_id'] for o in client.get('/api/workorders?batch=B2').json['orders']}
    assert first.isdisjoint(second)


def test_workorder_pane_renders_advice(client):
    page = client.get('/inspect/workorder')
    body = page.data.decode()
    assert page.status_code == 200
    assert '两波长均超限' in body and '仅长波超限' in body and '另一波长缺测' in body
    assert 'F-02' in body and '未派发' in body


def test_console_dispatch_claim_and_late_entry(client):
    assert client.post('/inspect/workorder', data={'batch': 'B1', 'sample': 'dual', 'action': 'dispatch'}).status_code == 200
    orders = client.get('/api/workorders?batch=B1').json['orders']
    order_id = next(o['summary']['order_id'] for o in orders if o['summary']['joint'] == 'F-01')
    claimed = client.post('/inspect/workorder', data={'batch': 'B1', 'sample': 'dual', 'action': 'claim',
                                                      'order': order_id, 'crew': 'crew-3'})
    assert '已领取 F-01' in claimed.data.decode()
    measured = client.post('/inspect/workorder', data={'batch': 'B1', 'sample': 'dual', 'action': 'measure',
                                                       'wave': 'long', 'source': 'b', 'rows': 'L-B5,540,m,0.10'})
    assert '已补录' in measured.data.decode()
    retried = client.post('/inspect/workorder', data={'batch': 'B1', 'sample': 'dual', 'action': 'measure',
                                                      'wave': 'long', 'source': 'b', 'rows': 'L-B5,540,m,0.10'})
    assert '重复提交' in retried.data.decode()
