import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from app.business import DomainIssue

SCHEMA = '''CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, report TEXT NOT NULL, created TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING', revision INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '', reviewed TEXT
);
CREATE TABLE IF NOT EXISTS sources (
    run_id TEXT PRIMARY KEY, data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS supplements (
    id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    request_data BLOB NOT NULL,
    state TEXT NOT NULL DEFAULT 'PENDING',
    result_run_id TEXT,
    decision TEXT,
    error_code TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE(base_run_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_supplements_base ON supplements(base_run_id, created);
CREATE TABLE IF NOT EXISTS run_versions (
    root_run_id TEXT NOT NULL,
    run_id TEXT PRIMARY KEY,
    base_run_id TEXT NOT NULL,
    supplement_id TEXT,
    version_no INTEGER NOT NULL,
    created TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_versions_root ON run_versions(root_run_id, version_no);
CREATE TABLE IF NOT EXISTS issuances (
    id TEXT PRIMARY KEY,
    supplement_id TEXT,
    run_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT,
    status TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created TEXT NOT NULL,
    UNIQUE(supplement_id, status)
);'''


class Store:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            db.executescript(SCHEMA)
            yield db

    def register(self, run_id, report, data=None):
        with self.connect() as db:
            db.execute('INSERT INTO runs(id,report,created) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING',
                       (run_id,json.dumps(report,ensure_ascii=False),report['manifest']['created']))
            if data is not None:
                db.execute('INSERT OR IGNORE INTO sources(run_id,data) VALUES (?,?)',(run_id,bytes(data)))
        return self.load(run_id)

    def save_source(self, run_id, data):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO sources(run_id,data) VALUES (?,?)',(run_id,bytes(data)))

    def load_source(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM sources WHERE run_id=?',(run_id,)).fetchone()
        return bytes(row['data']) if row is not None else None

    def update_report(self, run_id, report):
        with self.connect() as db:
            db.execute('UPDATE runs SET report=? WHERE id=?',
                       (json.dumps(report,ensure_ascii=False),run_id))
        return self.load(run_id)

    def load(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
        if row is None:
            raise DomainIssue('RUN_NOT_FOUND')
        result = dict(row)
        result['report'] = json.loads(result['report'])
        return result

    def list(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT id,created,status,revision,note FROM runs ORDER BY created DESC,id LIMIT 50')]

    def revise(self, run_id, revision, status, note, now):
        with self.connect() as db:
            cursor = db.execute('UPDATE runs SET status=?,note=?,reviewed=?,revision=revision+1 WHERE id=? AND revision=?',
                                (status,note,now,run_id,revision))
            if cursor.rowcount != 1:
                raise DomainIssue('STALE_REVIEW')
        return self.load(run_id)

    @staticmethod
    def _supplement(row):
        if row is None:
            return None
        item = dict(row)
        item['payload'] = json.loads(item.get('payload') or '{}')
        return item

    def insert_supplement(self, supplement):
        with self.connect() as db:
            db.execute('''INSERT INTO supplements
                (id,base_run_id,idempotency_key,request_sha256,request_data,state,
                 result_run_id,decision,error_code,payload,created,updated)
                VALUES (:id,:base_run_id,:idempotency_key,:request_sha256,:request_data,:state,
                 :result_run_id,:decision,:error_code,:payload,:created,:updated)
                ON CONFLICT(base_run_id,idempotency_key) DO NOTHING''',
                {**supplement,
                 'request_data': sqlite3.Binary(supplement['request_data']),
                 'payload': json.dumps(supplement.get('payload') or {},ensure_ascii=False)})
        found = self.find_supplement(supplement['base_run_id'],supplement['idempotency_key'])
        if found is None:
            raise DomainIssue('SUPPLEMENT_NOT_FOUND')
        return found

    def find_supplement(self, base_run_id, key):
        with self.connect() as db:
            row = db.execute('SELECT * FROM supplements WHERE base_run_id=? AND idempotency_key=?',
                              (base_run_id,key)).fetchone()
        return self._supplement(row)

    def load_supplement(self, supplement_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM supplements WHERE id=?',(supplement_id,)).fetchone()
        item = self._supplement(row)
        if item is None:
            raise DomainIssue('SUPPLEMENT_NOT_FOUND')
        return item

    def list_supplements(self, base_run_id=None):
        query = 'SELECT * FROM supplements'
        args = ()
        if base_run_id is not None:
            query += ' WHERE base_run_id=?'
            args = (base_run_id,)
        query += ' ORDER BY created DESC, id LIMIT 200'
        with self.connect() as db:
            rows = db.execute(query,args).fetchall()
        return [self._supplement(row) for row in rows]

    def claim_supplement(self, supplement_id, now):
        """Atomically claim a pending/interrupted/retryable operation."""
        with self.connect() as db:
            cursor = db.execute('''UPDATE supplements
                SET state='PROCESSING', error_code=NULL, updated=?
                WHERE id=? AND state IN ('PENDING','FAILED','INTERRUPTED','PROCESSING')''',
                (now,supplement_id))
            return cursor.rowcount == 1

    def update_supplement(self, supplement_id, **fields):
        if not fields:
            return self.load_supplement(supplement_id)
        allowed = {'state','result_run_id','decision','error_code','payload','updated'}
        values = {key:value for key,value in fields.items() if key in allowed}
        if 'payload' in values:
            values['payload'] = json.dumps(values['payload'],ensure_ascii=False)
        assignments = ', '.join(f'{key}=:{key}' for key in values)
        values['id'] = supplement_id
        with self.connect() as db:
            db.execute(f'UPDATE supplements SET {assignments} WHERE id=:id',values)
        return self.load_supplement(supplement_id)

    def add_version(self, root_run_id, run_id, base_run_id, supplement_id, version_no, now):
        with self.connect() as db:
            db.execute('''INSERT INTO run_versions
                (root_run_id,run_id,base_run_id,supplement_id,version_no,created)
                VALUES (?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING''',
                (root_run_id,run_id,base_run_id,supplement_id,version_no,now))

    def list_versions(self, root_run_id):
        with self.connect() as db:
            rows = db.execute('''SELECT rv.*, r.status, r.revision, r.note
                FROM run_versions rv JOIN runs r ON r.id=rv.run_id
                WHERE rv.root_run_id=? ORDER BY rv.version_no''',(root_run_id,)).fetchall()
        return [dict(row) for row in rows]

    def version_chain_for_run(self, run_id):
        with self.connect() as db:
            row = db.execute('SELECT root_run_id FROM run_versions WHERE run_id=?',(run_id,)).fetchone()
        if row is None:
            return []
        return self.list_versions(row['root_run_id'])

    def record_issuance(self, issuance_id, supplement_id, run_id, key, status, revision, now):
        with self.connect() as db:
            db.execute('''INSERT INTO issuances
                (id,supplement_id,run_id,idempotency_key,status,revision,created)
                VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO NOTHING''',
                (issuance_id,supplement_id,run_id,key,status,revision,now))
            row = db.execute('SELECT * FROM issuances WHERE run_id=?',(run_id,)).fetchone()
        return dict(row)

    def list_issuances(self, supplement_id=None):
        query = 'SELECT * FROM issuances'
        args = ()
        if supplement_id is not None:
            query += ' WHERE supplement_id=?'
            args = (supplement_id,)
        query += ' ORDER BY created DESC, id LIMIT 200'
        with self.connect() as db:
            return [dict(row) for row in db.execute(query,args).fetchall()]

# Naming aliases for alternate feature vocabulary.
Store.save_supplement = Store.insert_supplement
Store.get_supplement = Store.load_supplement
Store.create_supplement_record = Store.insert_supplement
Store.save_correction = Store.insert_supplement
Store.get_correction = Store.load_supplement
Store.list_corrections = Store.list_supplements
