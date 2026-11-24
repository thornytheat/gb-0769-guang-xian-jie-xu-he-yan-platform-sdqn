import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.errors import InspectionError

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    batch TEXT NOT NULL,
    joint TEXT NOT NULL,
    current_revision INTEGER NOT NULL,
    dispatched_at TEXT,
    claimed_revision INTEGER,
    claimed_by TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_order_revisions (
    order_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    recommendation TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    dispatched INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    document BLOB NOT NULL,
    PRIMARY KEY (order_id, revision)
);
CREATE INDEX IF NOT EXISTS work_orders_batch ON work_orders (batch);
CREATE TABLE IF NOT EXISTS work_order_batches (
    batch TEXT PRIMARY KEY,
    waves BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_order_ingests (
    batch TEXT NOT NULL,
    ingest_id TEXT NOT NULL,
    document BLOB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch, ingest_id)
);
CREATE TABLE IF NOT EXISTS work_order_measurements (
    batch TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ingest_id TEXT NOT NULL,
    wave TEXT NOT NULL,
    source TEXT NOT NULL,
    line TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (batch, seq)
);
CREATE TABLE IF NOT EXISTS work_order_claims (
    order_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    crew TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (order_id, revision, crew)
);
"""


@contextmanager
def session(database):
    """Serialized write transaction: dispatch and claim never interleave.

    ``executescript`` commits any open transaction, so the (idempotent) schema
    is created first and the explicit BEGIN IMMEDIATE follows it.
    """
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15, isolation_level=None)
    try:
        connection.execute('PRAGMA busy_timeout=15000')
        connection.executescript(SCHEMA)
        connection.execute('BEGIN IMMEDIATE')
        yield connection
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise
    finally:
        connection.close()


@contextmanager
def reader(database):
    """Read-only view; the schema is created by dispatch before any read."""
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=15, isolation_level=None)
    try:
        connection.execute('PRAGMA busy_timeout=15000')
        connection.executescript(SCHEMA)
        yield connection
    finally:
        connection.close()


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def encode(document):
    return json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def decode(blob):
    return json.loads(blob)


def insert_revision(db, order_id, revision, document, dispatched, stamp):
    db.execute('INSERT INTO work_order_revisions (order_id, revision, recommendation, reason, evidence_sha256, dispatched, created_at, document) '
               'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
               (order_id, revision, document['recommendation'], document['reason'],
                document['evidence_sha256'], int(dispatched), stamp, encode(document)))


def latest_revision(db, order_id):
    row = db.execute('SELECT revision, recommendation, reason, evidence_sha256, dispatched, created_at, document '
                     'FROM work_order_revisions WHERE order_id=? ORDER BY revision DESC LIMIT 1', (order_id,)).fetchone()
    if row is None:
        raise InspectionError('WORK_ORDER_MISSING_REVISION', status=500)
    return {'revision': row[0], 'recommendation': row[1], 'reason': row[2], 'evidence_sha256': row[3],
            'dispatched': bool(row[4]), 'created_at': row[5], 'document': decode(row[6])}


def revision_at(db, order_id, revision):
    row = db.execute('SELECT revision, recommendation, reason, evidence_sha256, dispatched, created_at, document '
                     'FROM work_order_revisions WHERE order_id=? AND revision=?', (order_id, revision)).fetchone()
    if row is None:
        raise InspectionError('WORK_ORDER_MISSING_REVISION', status=500)
    return {'revision': row[0], 'recommendation': row[1], 'reason': row[2], 'evidence_sha256': row[3],
            'dispatched': bool(row[4]), 'created_at': row[5], 'document': decode(row[6])}


def revision_list(db, order_id):
    rows = db.execute('SELECT revision, recommendation, reason, evidence_sha256, dispatched, created_at '
                      'FROM work_order_revisions WHERE order_id=? ORDER BY revision', (order_id,))
    return [{'revision': r[0], 'recommendation': r[1], 'reason': r[2], 'evidence_sha256': r[3],
             'dispatched': bool(r[4]), 'created_at': r[5]} for r in rows]


def order_row(db, order_id):
    row = db.execute('SELECT id, batch, joint, current_revision, dispatched_at, claimed_revision, claimed_by, '
                     'claimed_at, created_at FROM work_orders WHERE id=?', (order_id,)).fetchone()
    if row is None:
        return None
    keys = ('order_id', 'batch', 'joint', 'current_revision', 'dispatched_at', 'claimed_revision',
            'claimed_by', 'claimed_at', 'created_at')
    return dict(zip(keys, row))


def summary(order, current):
    document = current['document']
    return {'order_id': order['order_id'], 'batch': order['batch'], 'joint': order['joint'],
            'revision': current['revision'], 'recommendation': document['recommendation'],
            'reason': document['reason'], 'title': document['title'], 'instruction': document['instruction'],
            'position_mm': document['position_mm'], 'limit_mdb': _limit(document), 'net_db': _net(document),
            'evidence_sha256': document['evidence_sha256'],
            'dispatched_at': order['dispatched_at'], 'claimed_revision': order['claimed_revision'],
            'claimed_by': order['claimed_by'], 'claimed_at': order['claimed_at'],
            'changed_since_claim': order['claimed_revision'] is not None and
                                   order['claimed_revision'] != order['current_revision']}


def _limit(document):
    for wave in document.get('waves', {}).values():
        if wave.get('limit_mdb') is not None:
            return wave['limit_mdb']
    return None


def _net(document):
    """The over-limit wavelength's net loss, so a summary is self-explanatory."""
    for wave in document.get('waves', {}).values():
        if wave.get('state') == 'OVER_LIMIT':
            return wave.get('net_db')
    return None


def crew_rows(db, order_id):
    rows = db.execute('SELECT crew, revision, claimed_at FROM work_order_claims WHERE order_id=? '
                      'ORDER BY claimed_at, crew', (order_id,))
    return [{'crew': crew, 'revision': revision, 'claimed_at': claimed_at} for crew, revision, claimed_at in rows]
