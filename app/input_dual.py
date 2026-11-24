import json

from app.config import DATA, WAVE_KEYS
from app.errors import InspectionError


def dual(kind='dual'):
    folder = DATA / kind
    if not folder.is_dir():
        raise InspectionError('UNKNOWN_TRACE_SET', status=404)
    settings = json.loads((folder / 'link.json').read_text(encoding='utf-8'))
    return {key: {'traces': {source: (folder / key / f'{source}.csv').read_text(encoding='utf-8')
                             for source in ('a', 'b', 'design')},
                  'settings': settings, 'label': key} for key in WAVE_KEYS}
