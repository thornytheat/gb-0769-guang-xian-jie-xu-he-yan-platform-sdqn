from decimal import Decimal

from app.config import SPEC, WAVE_KEYS
from app.modules.archive.identity import canonical, digest
from app.pipeline import run

SHORT, LONG = WAVE_KEYS

PASS = 'PASS'
OVER = 'OVER_LIMIT'
MISSING = 'MISSING'

REFUSE_JOINT = 'REFUSE_JOINT'
CHECK_BEND = 'CHECK_BEND'
COMPLETE_MEASUREMENT = 'COMPLETE_MEASUREMENT'
WITHDRAW = 'WITHDRAW'
NONE = 'NONE'

# The recommendation answers "what may the crew do at the joint?", not merely
# "is the loss high?".  Two wavelengths over the limit is a joint defect; long
# wave alone looks like a bend, so re-melting would be the wrong first move.
REASONS = {
    'BOTH_WAVES_OVER': {
        'recommendation': REFUSE_JOINT,
        'title': '两波长均超限',
        'rationale': '短波与长波的双端均值都超过该接头上限，符合接头缺陷特征。',
        'instruction': '派发重熔工单：重熔或更换该接头，处置后补测两个波长。',
    },
    'LONG_WAVE_ONLY': {
        'recommendation': CHECK_BEND,
        'title': '仅长波超限',
        'rationale': '仅长波超限而短波通过，符合弯折损耗随波长增大的特征，不能按接头故障重熔。',
        'instruction': '派发弯折排查工单：检查弯曲半径、盘纤与固定点，处置后复测长波；确认弯折前禁止重熔。',
    },
    'SHORT_WAVE_ONLY': {
        'recommendation': REFUSE_JOINT,
        'title': '仅短波超限',
        'rationale': '仅短波超限而长波通过，不符合弯折损耗随波长增大的特征，优先按接头缺陷核查。',
        'instruction': '派发接头核查工单：复核端面与熔接质量，必要时重熔；处置后复测两个波长。',
    },
    'OTHER_WAVE_UNMEASURED': {
        'recommendation': COMPLETE_MEASUREMENT,
        'title': '另一波长缺测',
        'rationale': '缺一端的波长按缺测处理，不能作为判据；此时重熔可能把弯折误判为接头故障。',
        'instruction': '出车前先派发排障工单，但必须先补测缺测波长的双端读数；补测完成前禁止重熔。',
    },
}

# Joints whose evidence does not support sending a crew out.  These carry
# recommendation NONE in a fresh evaluation; a stored order that reaches one is
# withdrawn (recommendation WITHDRAW) so a crew already holding it sees why.
WITHDRAWALS = {
    'NO_OVER_LIMIT': {
        'title': '两波长均未超限',
        'rationale': '两波长的双端均值均未超出该接头上限，不符合排障派发条件。',
        'instruction': '无需派发；已派发的工单撤回，原领取依据仍可按版本追溯。',
    },
    'WAVE_MISSING_NO_VERDICT': {
        'title': '仅存波长未超限，另一波长缺测',
        'rationale': '已测波长未超限，另一波长缺测，现有证据不足以支撑排障派发。',
        'instruction': '暂不派发；如需判断，先补齐两个波长的双端读数。',
    },
    'BOTH_WAVES_UNMEASURED': {
        'title': '两波长均缺测',
        'rationale': '两个波长都没有完整的双端读数，无法支撑排障派发。',
        'instruction': '暂不派发；补齐两个波长的双端读数后重跑。',
    },
    'JOINT_ABSENT': {
        'title': '设计清单已无此接头',
        'rationale': '本次对账的设计清单中已找不到该接头，原排障对象不存在。',
        'instruction': '撤回原工单；核对设计清单后再决定是否重新派发。',
    },
}

RECOMMENDATIONS = (REFUSE_JOINT, CHECK_BEND, COMPLETE_MEASUREMENT, WITHDRAW, NONE)


def detail(reason):
    """Text for a reason, whether the joint is dispatched or not."""
    return REASONS.get(reason) or WITHDRAWALS[reason]


# Recommendations that put a crew on the road.
DISPATCHED = (REFUSE_JOINT, CHECK_BEND, COMPLETE_MEASUREMENT)


def _db(mdb):
    return None if mdb is None else f'{Decimal(mdb) / 1000:.4f}'


def _wave(row, label):
    """One wavelength's verdict for one design joint.

    Only PASS and OVER_LIMIT count as a verdict.  Single-ended, orphan or
    rejected evidence keeps the wavelength at MISSING (缺测): without both
    ends a wavelength is unmeasured, never passed.
    """
    if row is None:
        return {'label': label, 'present': False, 'state': MISSING, 'code': 'NO_DESIGN_POINT',
                'net_db': None, 'limit_db': None, 'limit_mdb': None, 'design_line': None, 'chain_mm': None,
                'pair': None, 'a_line': None, 'b_line': None, 'near_singles': [], 'gain_ends': []}
    state = row['code'] if row['code'] in (PASS, OVER) else MISSING
    return {'label': label, 'present': True, 'state': state, 'code': row['code'],
            'net_db': row['net_db'], 'limit_db': _db(row['limit_mdb']), 'limit_mdb': row['limit_mdb'],
            'design_line': row['design_line'], 'chain_mm': row['chain_mm'],
            'pair': row['pair'], 'a_line': row['a_line'], 'b_line': row['b_line'],
            'near_singles': [{'id': s['id'], 'source': s['source'], 'line': s['line']} for s in row['near_singles']],
            'gain_ends': list(row['gain_ends'])}


def classify(short_state, long_state):
    """Return (recommendation, reason) from the two per-wave states.

    Any missing wavelength wins over a verdict: an over-limit reading with the
    other wavelength still unmeasured earns a 补测 order, not a re-melt.
    """
    states = (short_state, long_state)
    over = [key for key, state in zip(WAVE_KEYS, states) if state == OVER]
    missing = [key for key, state in zip(WAVE_KEYS, states) if state == MISSING]
    if not over:
        if len(missing) == 2:
            return NONE, 'BOTH_WAVES_UNMEASURED'
        if missing:
            return NONE, 'WAVE_MISSING_NO_VERDICT'
        return NONE, 'NO_OVER_LIMIT'
    if missing:
        return COMPLETE_MEASUREMENT, 'OTHER_WAVE_UNMEASURED'
    if short_state == OVER and long_state == OVER:
        return REFUSE_JOINT, 'BOTH_WAVES_OVER'
    if long_state == OVER:
        return CHECK_BEND, 'LONG_WAVE_ONLY'
    return REFUSE_JOINT, 'SHORT_WAVE_ONLY'


def order_id(batch, joint):
    """Stable per batch+joint, so a re-run maps back to the same work order."""
    return digest(canonical([SPEC, 'work-order', batch, joint]))


def _position(evidence):
    for key in (SHORT, LONG):
        if evidence[key]['chain_mm'] is not None:
            return evidence[key]['chain_mm']
    return None


def _signature(evidence):
    """What a revision is keyed on: the judgement, not raw provenance lines.

    A design listing that gains or loses an unrelated row shifts every line
    number; that alone must not open a new revision on each surviving joint.
    Event ids, chain position and paired values are stable evidence identities.
    """
    return {key: {'label': wave['label'], 'state': wave['state'], 'code': wave['code'],
                  'net_db': wave['net_db'], 'limit_mdb': wave['limit_mdb'], 'chain_mm': wave['chain_mm'],
                  'pair': wave['pair'], 'near_singles': [(s['id'], s['source']) for s in wave['near_singles']],
                  'gain_ends': wave['gain_ends']} for key, wave in evidence.items()}


def evaluate(batch, waves):
    """Reconcile both wavelengths against their design joints and decide.

    ``waves`` maps 'short'/'long' to {'traces', 'settings', 'label'}.  Each
    wavelength runs through the normal double-ended pipeline on its own trace
    set and design listing; the two reconciliations are then joined per joint.
    """
    outputs = {key: run({'traces': waves[key]['traces'], 'settings': waves[key]['settings']}, 'design')
               for key in WAVE_KEYS}
    designs = {key: {row['joint']: row for row in outputs[key]['design']['rows']} for key in WAVE_KEYS}
    labels = {key: waves[key]['label'] for key in WAVE_KEYS}
    joints = set(designs[SHORT]) | set(designs[LONG])
    ordered = sorted(joints, key=lambda joint: (
        min((designs[key][joint]['chain_mm'] for key in WAVE_KEYS if joint in designs[key]), default=0), joint))
    settings = {key: outputs[key]['calibration']['settings'] for key in WAVE_KEYS}
    design = {key: {'design_count': outputs[key]['design']['design_count'],
                    'over_limit': outputs[key]['design']['over_limit'],
                    'missing': outputs[key]['design']['missing'],
                    'counts': outputs[key]['trace']['counts'],
                    'reason_counts': outputs[key]['trace']['reason_counts'],
                    'isolated': outputs[key]['trace']['isolated']} for key in WAVE_KEYS}
    decisions = []
    for joint in ordered:
        evidence = {key: _wave(designs[key].get(joint), labels[key]) for key in WAVE_KEYS}
        recommendation, reason = classify(evidence[SHORT]['state'], evidence[LONG]['state'])
        text = detail(reason)
        evidence_sha = digest(canonical({'batch': batch, 'joint': joint, 'recommendation': recommendation,
                                         'reason': reason, 'waves': _signature(evidence)}))
        decisions.append({'joint': joint, 'order_id': order_id(batch, joint),
                          'position_mm': _position(evidence), 'recommendation': recommendation, 'reason': reason,
                          'title': text['title'], 'rationale': text['rationale'],
                          'instruction': text['instruction'], 'waves': evidence,
                          'evidence_sha256': evidence_sha, 'settings': settings, 'design': design})
    counts = {code: sum(d['recommendation'] == code for d in decisions) for code in RECOMMENDATIONS}
    return {'batch': batch, 'labels': labels, 'joints': decisions, 'counts': counts,
            'dispatchable': sum(d['recommendation'] in DISPATCHED for d in decisions),
            'settings': settings, 'design': design}


def absent(batch, joint, previous_sha):
    """Withdrawal basis for a joint that vanished from both design listings."""
    text = WITHDRAWALS['JOINT_ABSENT']
    payload = {'batch': batch, 'joint': joint, 'recommendation': WITHDRAW, 'reason': 'JOINT_ABSENT',
               'previous_evidence_sha256': previous_sha}
    return {'joint': joint, 'order_id': order_id(batch, joint), 'position_mm': None,
            'recommendation': WITHDRAW, 'reason': 'JOINT_ABSENT', 'title': text['title'],
            'rationale': text['rationale'], 'instruction': text['instruction'],
            'waves': {}, 'evidence_sha256': digest(canonical(payload)), 'settings': {}, 'design': {}}
